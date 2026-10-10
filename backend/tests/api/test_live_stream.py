"""The live well feed over the real ASGI application.

Every assertion here is about a failure mode that a naive socket implementation has: a reconnect that
misses events, a gap that is not announced, a burst of measurements that becomes a burst of frames, an
alert lost to coalescing, a socket that keeps tailing the database for a browser that closed, and a well
belonging to another organisation.

The clock is compressed (a fifty-millisecond tail interval) so the tests exercise the real poll loop
rather than a mock of it; the heartbeat interval is pushed out except in the one test that is about
heartbeats, because interleaving them everywhere would make the ordering assertions about the test's
timing rather than about the feed's.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import select
from tests.api.conftest import headers
from tests.api.ws import WebSocketRefused

from drillai.core.config import reset_settings_cache
from drillai.db.models import Organization, Project, Well
from drillai.telemetry.vocabulary import COALESCIBLE_EVENT_TYPES

PREFIX = "/api/v1"
BASE_TS = dt.datetime(2026, 3, 15, 12, 0, tzinfo=dt.UTC)


@pytest.fixture(autouse=True)
def _fast_feed(monkeypatch, api_settings):
    """Tail quickly, and without heartbeats unless a test asks for them."""

    monkeypatch.setenv("DRILLAI_LIVE_STREAM_POLL_SECONDS", "0.05")
    monkeypatch.setenv("DRILLAI_LIVE_STREAM_HEARTBEAT_SECONDS", "3600")
    reset_settings_cache()
    yield
    reset_settings_cache()


async def _well_with_channel(client, *, channel_key: str = "spp", unit: str = "psi") -> tuple[str, str]:
    project = await client.post(
        f"{PREFIX}/projects", json={"name": "Live feed"}, headers=headers("well_manager")
    )
    well = await client.post(
        f"{PREFIX}/wells",
        json={"project_id": project.json()["id"], "name": "LIVE-1", "well_type": "development_producer"},
        headers=headers("well_manager"),
    )
    assert well.status_code == 201, well.text
    channel = await client.post(
        f"{PREFIX}/timeseries",
        json={
            "well_id": well.json()["id"],
            "channel_key": channel_key,
            "name": "Standpipe pressure",
            "dimension": "pressure",
            "unit": unit,
            "source": "witsml",
        },
        headers=headers("engineer"),
    )
    assert channel.status_code == 201, channel.text
    return well.json()["id"], channel.json()["id"]


async def _append(client, series_id: str, points: list[dict]) -> dict:
    response = await client.post(
        f"{PREFIX}/timeseries/{series_id}/points",
        json={"points": points},
        headers=headers("engineer"),
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _rule(client, well_id: str, **overrides) -> dict:
    body = {
        "rule_key": "spp-high",
        "name": "Standpipe pressure high",
        "channel_key": "spp",
        "operator": "gt",
        "threshold": 4000.0,
        "unit": "psi",
        "severity": "high",
        "well_id": well_id,
        **overrides,
    }
    response = await client.post(f"{PREFIX}/alert-rules", json=body, headers=headers("engineer"))
    assert response.status_code == 201, response.text
    return response.json()


async def _read(socket, count: int) -> list[dict]:
    return [await socket.receive() for _ in range(count)]


async def _read_until(socket, predicate, *, limit: int = 60) -> list[dict]:
    """Read frames until ``predicate`` is true of the frame just read (or the socket refuses)."""

    frames: list[dict] = []
    for _ in range(limit):
        frame = await socket.receive()
        frames.append(frame)
        if predicate(frame):
            return frames
    raise AssertionError(f"no frame matched after {limit} frames: {[f['type'] for f in frames]}")


def _sequences(frames: list[dict]) -> list[int]:
    """Every sequence number the frames carry, in the order they were sent."""

    seen: list[int] = []
    for frame in frames:
        if frame["type"] == "event":
            seen.append(frame["event"]["sequence"])
        elif frame["type"] == "telemetry":
            seen.extend(entry["sequence"] for entry in frame["channels"])
        elif frame["type"] == "gap":
            continue
        else:
            continue
    return seen


# --------------------------------------------------------------------------- hello and snapshot


async def test_the_stream_opens_with_a_position_and_a_bounded_snapshot(client, websocket) -> None:
    well_id, series_id = await _well_with_channel(client)
    # Measured seconds ago, so the snapshot's freshness is the real answer for a live well rather than
    # "stale" — the point of the frame is that a screen can tell the two apart.
    recent = dt.datetime.now(dt.UTC) - dt.timedelta(seconds=2)
    await _append(client, series_id, [{"ts": recent.isoformat(), "value": 4200.0, "unit": "psi"}])
    await _rule(client, well_id)
    evaluated = await client.post(
        f"{PREFIX}/wells/{well_id}/alerts/evaluate", headers=headers("engineer")
    )
    assert evaluated.json()["raised"] == 1

    async with websocket(f"{PREFIX}/wells/{well_id}/live/stream") as socket:
        opened = await socket.receive()
        assert opened["type"] == "stream_opened"
        assert opened["well_id"] == well_id
        assert opened["position"] >= 2, "the stream already holds the channel, the batch and the alert"
        assert opened["schema_version"] == 1
        assert opened["after_seq"] == 0
        assert opened["poll_seconds"] == pytest.approx(0.05)
        assert set(opened["coalesced_types"]) == set(COALESCIBLE_EVENT_TYPES)
        for frame_type in ("snapshot", "event", "telemetry", "gap", "heartbeat"):
            assert frame_type in opened["frame_types"]

        snapshot = await socket.receive()
        assert snapshot["type"] == "snapshot"
        assert snapshot["well_id"] == well_id
        assert snapshot["generated_at"]
        assert [reading["channel_key"] for reading in snapshot["latest"]] == ["spp"]
        reading = snapshot["latest"][0]
        assert reading["value"] == pytest.approx(4200.0 * 6894.757293168)
        assert reading["unit"] == "Pa"
        assert reading["freshness"] == "fresh"
        assert reading["age_seconds"] is not None
        assert snapshot["freshness"] == {"fresh": 1}
        assert len(snapshot["alerts"]) == 1
        assert snapshot["alerts"][0]["status"] == "raised"
        assert snapshot["alerts"][0]["severity"] == "high"


# --------------------------------------------------------------------------- coalescing


async def test_telemetry_is_coalesced_and_alerts_are_not(client, websocket) -> None:
    well_id, series_id = await _well_with_channel(client)
    await _rule(client, well_id)

    async with websocket(f"{PREFIX}/wells/{well_id}/live/stream") as socket:
        await _read(socket, 2)  # hello + snapshot

        # Four measurements in one batch: one `telemetry` frame folding four events, and the alert the
        # batch raised beside it — never inside it.
        await _append(
            client,
            series_id,
            [
                {"ts": (BASE_TS + dt.timedelta(seconds=index)).isoformat(), "value": 4100.0 + index, "unit": "psi"}
                for index in range(4)
            ],
        )
        frames = await _read_until(
            socket,
            lambda frame: frame["type"] == "event" and frame["event"]["type"] == "alert.raised",
        )
        telemetry_frames = [frame for frame in frames if frame["type"] == "telemetry"]
        assert len(telemetry_frames) == 1, [frame["type"] for frame in frames]
        folded = telemetry_frames[0]
        assert folded["count"] == 1, "one batch is one telemetry.received event"
        assert folded["omitted"] == 0

        event_frames = [frame for frame in frames if frame["type"] == "event"]
        types = [frame["event"]["type"] for frame in event_frames]
        assert "well_state.changed" in types, "the first measurement on the channel is a state change"
        assert "alert.raised" in types, types

        # A second breaching batch while the alert is already open: it is telemetry and nothing else —
        # no second alert, and no state change (the channel was already live).
        await _append(
            client,
            series_id,
            [
                {"ts": (BASE_TS + dt.timedelta(seconds=10)).isoformat(), "value": 4300.0, "unit": "psi"},
                {"ts": (BASE_TS + dt.timedelta(seconds=11)).isoformat(), "value": 4310.0, "unit": "psi"},
            ],
        )
        rest = await _read_until(socket, lambda frame: frame["type"] == "telemetry")
        assert [frame["type"] for frame in rest] == ["telemetry"], [f["type"] for f in rest]
        assert rest[0]["channels"][0]["sequence"] > folded["channels"][-1]["sequence"]

        # An alert arriving *between* telemetry bursts is not folded into one: the sequence order the
        # client applies is the order the events happened in.
        alert_frame = next(
            frame for frame in frames if frame["type"] == "event" and frame["event"]["type"] == "alert.raised"
        )
        sequence_order = _sequences(frames + rest)
        assert sequence_order == sorted(sequence_order), "frames keep the stream's order"
        assert alert_frame["event"]["sequence"] > folded["channels"][-1]["sequence"]

        # The alert frame carries the alert's own identity: a client can deep-link and refetch it.
        alert_frame = next(
            frame for frame in frames if frame["type"] == "event" and frame["event"]["type"] == "alert.raised"
        )
        assert alert_frame["event"]["subject"]["kind"] == "alert"
        assert alert_frame["event"]["payload"]["severity"] == "high"
        assert alert_frame["event"]["schema_version"] == 1


async def test_several_telemetry_events_fold_into_one_frame_with_a_resync_flag(client, websocket) -> None:
    well_id, series_id = await _well_with_channel(client)

    for index in range(3):
        await _append(
            client,
            series_id,
            [{"ts": (BASE_TS + dt.timedelta(seconds=index)).isoformat(), "value": 4000.0 + index, "unit": "psi"}],
        )

    # Connect with a cursor behind all three so they are served in one page.
    async with websocket(f"{PREFIX}/wells/{well_id}/live/stream", after_seq="0") as socket:
        await _read(socket, 2)
        frames = await _read_until(socket, lambda frame: frame["type"] == "telemetry")
        folded = frames[-1]
        assert folded["count"] == 3 and folded["omitted"] == 2
        assert folded["resync"] is True, "a folded burst tells the client to re-read, not to guess"
        assert [entry["channel_key"] for entry in folded["channels"]] == ["spp"] * 3
        assert folded["from"] == folded["channels"][0]["sequence"]
        assert folded["to"] == folded["channels"][-1]["sequence"]


# --------------------------------------------------------------------------- resume and gaps


async def test_reconnecting_resumes_from_the_cursor_without_duplicates(client, websocket) -> None:
    well_id, series_id = await _well_with_channel(client)
    await _rule(client, well_id)

    async with websocket(f"{PREFIX}/wells/{well_id}/live/stream") as first:
        await _read(first, 2)
        await _append(
            client,
            series_id,
            [{"ts": BASE_TS.isoformat(), "value": 4200.0, "unit": "psi"}],
        )
        frames = await _read_until(first, lambda frame: frame["type"] == "event")
        seen = _sequences(frames)
        assert seen, "the first connection must have delivered something"
        cursor = max(seen)
        first_ids = [frame["event"]["id"] for frame in frames if frame["type"] == "event"]

    # Something happens while nobody is listening — which is the whole reason a cursor exists.
    await _append(
        client,
        series_id,
        [{"ts": (BASE_TS + dt.timedelta(minutes=1)).isoformat(), "value": 4400.0, "unit": "psi"}],
    )

    async with websocket(
        f"{PREFIX}/wells/{well_id}/live/stream", after_seq=str(cursor)
    ) as second:
        opened = await second.receive()
        assert opened["after_seq"] == cursor
        assert opened["position"] > cursor
        await second.receive()  # snapshot
        resumed = await _read_until(second, lambda frame: frame["type"] == "event")

    resumed_sequences = _sequences(resumed)
    assert min(resumed_sequences) == cursor + 1, "the resume continues exactly where the cursor was"
    resumed_ids = [frame["event"]["id"] for frame in resumed if frame["type"] == "event"]
    assert set(resumed_ids).isdisjoint(first_ids), "nothing is delivered twice"


async def test_a_cursor_ahead_of_the_stream_is_announced_as_a_gap(client, websocket) -> None:
    well_id, _series_id = await _well_with_channel(client)

    async with websocket(f"{PREFIX}/wells/{well_id}/live/stream", after_seq="1000000") as socket:
        await _read(socket, 2)
        frames = await _read_until(socket, lambda frame: frame["type"] == "gap")
    gap = frames[-1]
    assert gap["reason"] == "cursor_ahead_of_stream"
    assert gap["requested_after"] == 1000000
    assert gap["resync"] is True
    assert gap["position"] < 1000000


async def test_a_client_beyond_the_backlog_is_told_the_range_it_lost(
    client, websocket, monkeypatch
) -> None:
    monkeypatch.setenv("DRILLAI_LIVE_STREAM_MAX_BACKLOG", "1")
    reset_settings_cache()
    well_id, series_id = await _well_with_channel(client)
    for index in range(4):
        await _append(
            client,
            series_id,
            [{"ts": (BASE_TS + dt.timedelta(seconds=index)).isoformat(), "value": 4000.0, "unit": "psi"}],
        )

    async with websocket(f"{PREFIX}/wells/{well_id}/live/stream", after_seq="0") as socket:
        await _read(socket, 2)
        frames = await _read_until(socket, lambda frame: frame["type"] == "gap")
    gap = frames[-1]
    assert gap["reason"] == "backlog_exceeded"
    assert gap["from"] == 1, "the gap names the first event the client did not get"
    assert gap["to"] == gap["position"]
    assert gap["omitted"] == gap["position"]
    assert gap["resync"] is True


async def test_a_quiet_stream_still_says_where_it_is(client, websocket, monkeypatch) -> None:
    monkeypatch.setenv("DRILLAI_LIVE_STREAM_HEARTBEAT_SECONDS", "0.15")
    reset_settings_cache()
    well_id, series_id = await _well_with_channel(client)
    await _append(client, series_id, [{"ts": BASE_TS.isoformat(), "value": 3900.0, "unit": "psi"}])

    async with websocket(f"{PREFIX}/wells/{well_id}/live/stream", after_seq="0") as socket:
        await _read(socket, 2)
        frames = await _read_until(socket, lambda frame: frame["type"] == "heartbeat")
    heartbeat = frames[-1]
    assert heartbeat["position"] >= 1
    assert heartbeat["stream_position"] == heartbeat["position"]
    assert heartbeat["server_time"]


# --------------------------------------------------------------------------- isolation and refusal


async def test_another_organisations_well_closes_the_socket_as_not_found(client, websocket, db) -> None:
    """The well exists; it belongs to somebody else. That is the same answer as a well that does not
    exist, on purpose — an identifier must not be a tenant capability."""

    async with db.session() as session:
        session.add(
            Organization(
                id="org_foreign",
                slug="foreign-live",
                name="Foreign Operator",
                kind="operator",
                timezone="UTC",
                default_unit_system="metric",
                default_locale="en",
                is_active=True,
            )
        )
        await session.flush()
        session.add(
            Project(
                id="prj_foreign",
                org_id="org_foreign",
                name="Foreign project",
                status="active",
                datum_policy="rkb",
                settings={},
            )
        )
        await session.flush()
        session.add(
            Well(
                id="wel_foreign",
                org_id="org_foreign",
                project_id="prj_foreign",
                name="FOREIGN-1",
                well_type="development_producer",
                elevation_datum="msl",
                is_offshore=False,
                twin_state="none",
                tags=[],
                attributes={},
                is_demo_fixture=False,
            )
        )
        await session.commit()

    with pytest.raises(WebSocketRefused) as refused:
        async with websocket(f"{PREFIX}/wells/wel_foreign/live/stream") as socket:
            await socket.receive()
    assert refused.value.code == 4404

    # A well that does not exist at all is refused identically.
    with pytest.raises(WebSocketRefused) as missing:
        async with websocket(f"{PREFIX}/wells/wel_does_not_exist/live/stream") as socket:
            await socket.receive()
    assert missing.value.code == 4404


async def test_a_handshake_that_cannot_be_authenticated_is_refused(client, websocket) -> None:
    """An identity the catalogue does not know is *unauthenticated* (4401), which is a different answer
    from "authenticated but not permitted" (4403) — a client that cannot tell them apart cannot know
    whether to re-authenticate or to ask for a role."""

    well_id, _series_id = await _well_with_channel(client)
    with pytest.raises(WebSocketRefused) as refused:
        async with websocket(
            f"{PREFIX}/wells/{well_id}/live/stream", dev_roles="not_a_catalogue_role"
        ) as socket:
            await socket.receive()
    assert refused.value.code == 4401
    assert "not authorized" in (refused.value.reason or "")


def test_the_live_read_gate_is_a_real_gate() -> None:
    """``live.read`` is checked before anything is read, and it is not a permission every principal has:
    a principal with no roles holds nothing, which is the case the socket refuses with 4403."""

    from drillai.security.actions import Principal
    from drillai.security.rbac import ROLE_CATALOGUE, permission_granted

    anonymous = Principal(id="usr_nobody")
    assert permission_granted(anonymous, "live.read") is False

    # Every catalogued role that may look at a well may follow its live state — the operational monitor
    # is a read surface, and a role that can read the well but not watch it would be a role that sees
    # yesterday's numbers on a live rig. The gate exists for principals assembled outside the catalogue
    # (a service token, an integration) and for roles added later.
    for role in ROLE_CATALOGUE:
        principal = Principal(id="usr_x", permissions=frozenset(role.permissions))
        assert permission_granted(principal, "live.read") is True, role.key


async def test_the_handler_stops_by_itself_when_the_client_leaves(client, websocket) -> None:
    """No client, no tail: an abandoned socket must not keep reading the outbox."""

    well_id, _series_id = await _well_with_channel(client)
    async with websocket(f"{PREFIX}/wells/{well_id}/live/stream") as socket:
        await _read(socket, 2)
        assert _sequences([]) == []
    assert socket.handler_state == "returned", "the handler noticed the disconnect and ended"


async def test_the_stream_is_scoped_to_its_well(client, websocket, db) -> None:
    """Events on another well of the same organisation are not sent to this socket (and are not lost —
    the other well's own stream, or a REST read, has them)."""

    well_id, series_id = await _well_with_channel(client)
    other_well_id, other_series = await _well_with_channel(client, channel_key="wob")
    assert other_well_id != well_id

    await _append(client, series_id, [{"ts": BASE_TS.isoformat(), "value": 3900.0, "unit": "psi"}])
    await _append(client, other_series, [{"ts": BASE_TS.isoformat(), "value": 3900.0, "unit": "psi"}])

    async with websocket(f"{PREFIX}/wells/{well_id}/live/stream", after_seq="0") as socket:
        await _read(socket, 2)
        frames = await _read_until(socket, lambda frame: frame["type"] in {"event", "telemetry"})
        payloads = [
            frame["event"]["payload"]
            for frame in frames
            if frame["type"] == "event"
        ]
    channels = {payload.get("channel_key") for payload in payloads if "channel_key" in payload}
    assert channels <= {"spp"}, channels

    async with db.session() as session:
        rows = list((await session.execute(select(Well.id).order_by(Well.id))).scalars())
    assert set(rows) >= {well_id, other_well_id}


async def test_interleaved_activity_on_another_well_never_triggers_false_backlog_gap(
    client, websocket, monkeypatch
) -> None:
    """P0 regression: Well B emitting many events (> live_stream_max_backlog) between two events on
    Well A must not trigger a false `backlog_exceeded` gap or drop an alert on Well A."""

    monkeypatch.setenv("DRILLAI_LIVE_STREAM_MAX_BACKLOG", "3")
    reset_settings_cache()

    well_a, series_a = await _well_with_channel(client, channel_key="spp", unit="psi")
    _well_b, series_b = await _well_with_channel(client, channel_key="choke_pressure", unit="psi")
    await _rule(client, well_a)

    # Initial point on Well A so the client holds a non-zero cursor on Well A.
    await _append(client, series_a, [{"ts": BASE_TS.isoformat(), "value": 3500.0, "unit": "psi"}])

    async with websocket(f"{PREFIX}/wells/{well_a}/live/stream") as socket_a:
        opened = await socket_a.receive()
        assert opened["type"] == "stream_opened"
        assert opened["cursor_scope"]["well_id"] == well_a
        assert opened["cursor_scope"]["sequence_space"] == "well"
        cursor_a = opened["position"]
        assert cursor_a >= 1
        await socket_a.receive()  # snapshot

        # Emit 8 batches on Well B (far exceeding backlog_bound = 3 in global org sequence).
        for idx in range(8):
            await _append(
                client,
                series_b,
                [
                    {
                        "ts": (BASE_TS + dt.timedelta(seconds=idx + 1)).isoformat(),
                        "value": 100.0 + idx,
                        "unit": "psi",
                    }
                ],
            )

        # Now breach the threshold on Well A and raise an alert automatically on ingest.
        await _append(
            client,
            series_a,
            [
                {
                    "ts": (BASE_TS + dt.timedelta(seconds=30)).isoformat(),
                    "value": 4500.0,
                    "unit": "psi",
                }
            ],
        )

        frames = await _read_until(
            socket_a,
            lambda frame: frame["type"] == "event" and frame["event"]["type"] == "alert.raised",
        )

    gap_frames = [frame for frame in frames if frame["type"] == "gap"]
    assert gap_frames == [], f"false gap triggered by Well B activity: {gap_frames}"
    alert_frame = next(
        frame for frame in frames if frame["type"] == "event" and frame["event"]["type"] == "alert.raised"
    )
    assert alert_frame["event"]["well_id"] == well_a
    assert alert_frame["event"]["sequence"] > cursor_a
    assert alert_frame["event"]["org_sequence"] > alert_frame["event"]["sequence"]


async def test_foreign_well_cursor_and_invalid_cursor_are_reported_as_gaps_not_silently_skipped(
    client, websocket
) -> None:
    """Presenting a cursor from Well B on Well A's stream (even when Well B's sequence is smaller than
    Well A's position) emits an explicit `cursor_scope_mismatch` gap and forces resync."""

    well_a, series_a = await _well_with_channel(client, channel_key="spp", unit="psi")
    well_b, series_b = await _well_with_channel(client, channel_key="choke_pressure", unit="psi")

    for idx in range(3):
        await _append(
            client,
            series_a,
            [{"ts": (BASE_TS + dt.timedelta(seconds=idx)).isoformat(), "value": 3600.0 + idx, "unit": "psi"}],
        )
    await _append(client, series_b, [{"ts": BASE_TS.isoformat(), "value": 90.0, "unit": "psi"}])

    async with websocket(f"{PREFIX}/wells/{well_b}/live/stream") as socket_b:
        opened_b = await socket_b.receive()
        cursor_from_well_b = opened_b["cursor"]
        assert opened_b["position"] < 4

    # Present Well B's cursor when connecting to Well A
    async with websocket(
        f"{PREFIX}/wells/{well_a}/live/stream", cursor=cursor_from_well_b
    ) as socket_a:
        await _read(socket_a, 2)  # stream_opened + snapshot
        gap = await socket_a.receive()
    assert gap["type"] == "gap"
    assert gap["reason"] == "cursor_scope_mismatch"
    assert gap["well_id"] == well_a
    assert gap["resync"] is True
    assert gap["details"]["expected_well_id"] == well_a
    assert gap["details"]["cursor_well_id"] == well_b

    # Present a corrupt cursor token
    async with websocket(
        f"{PREFIX}/wells/{well_a}/live/stream", cursor="corrupt_token"
    ) as socket_bad:
        await _read(socket_bad, 2)
        bad_gap = await socket_bad.receive()
    assert bad_gap["type"] == "gap"
    assert bad_gap["reason"] == "invalid_cursor"
    assert bad_gap["resync"] is True


async def test_scoped_cursor_resumes_and_multiple_consumers_observe_deterministic_order(
    client, websocket
) -> None:
    """Two simultaneous subscribers on the same well see identical sequence order, and reconnecting
    with the opaque `cursor` token resumes without duplicates."""

    well_id, series_id = await _well_with_channel(client)
    await _rule(client, well_id)

    async with (
        websocket(f"{PREFIX}/wells/{well_id}/live/stream") as sub_one,
        websocket(f"{PREFIX}/wells/{well_id}/live/stream") as sub_two,
    ):
        await _read(sub_one, 2)
        await _read(sub_two, 2)

        await _append(
            client,
            series_id,
            [{"ts": BASE_TS.isoformat(), "value": 4250.0, "unit": "psi"}],
        )

        frames_one = await _read_until(
            sub_one, lambda frame: frame["type"] == "event" and frame["event"]["type"] == "alert.raised"
        )
        frames_two = await _read_until(
            sub_two, lambda frame: frame["type"] == "event" and frame["event"]["type"] == "alert.raised"
        )
        assert _sequences(frames_one) == _sequences(frames_two)
        resume_cursor = frames_one[-1]["cursor"]

    # Emit one more batch while disconnected and resume via `cursor=`
    await _append(
        client,
        series_id,
        [{"ts": (BASE_TS + dt.timedelta(seconds=20)).isoformat(), "value": 4300.0, "unit": "psi"}],
    )

    async with websocket(f"{PREFIX}/wells/{well_id}/live/stream", cursor=resume_cursor) as resumed:
        await _read(resumed, 2)
        tail_frames = await _read_until(resumed, lambda frame: frame["type"] == "telemetry")
    assert _sequences(tail_frames) == [max(_sequences(frames_one)) + 1]


async def test_synthetic_commissioning_and_live_snapshot_endpoint(client, websocket) -> None:
    """POST /wells/{well_id}/timeseries/commission-synthetic runs SyntheticAdapter via AdapterRunner,
    auto-raises alerts, emits outbox events to the live stream, and GET /wells/{well_id}/live/snapshot
    returns the assembled operational view with explicit as_of timestamps and cursor."""

    project = await client.post(
        f"{PREFIX}/projects", json={"name": "Commissioning"}, headers=headers("well_manager")
    )
    well = await client.post(
        f"{PREFIX}/wells",
        json={"project_id": project.json()["id"], "name": "COMM-1", "well_type": "development_producer"},
        headers=headers("well_manager"),
    )
    assert well.status_code == 201, well.text
    well_id = well.json()["id"]

    rule_resp = await client.post(
        f"{PREFIX}/alert-rules",
        json={
            "rule_key": "spp-high-comm",
            "name": "Standpipe Pressure High",
            "channel_key": "spp",
            "well_id": well_id,
            "operator": "gt",
            "threshold": 4000.0,
            "unit": "psi",
            "severity": "critical",
            "sustain_seconds": 5.0,
            "cooldown_seconds": 0.0,
        },
        headers=headers("engineer"),
    )
    assert rule_resp.status_code == 201, rule_resp.text

    async with websocket(f"{PREFIX}/wells/{well_id}/live/stream") as socket:
        await _read(socket, 2)
        comm_resp = await client.post(
            f"{PREFIX}/wells/{well_id}/timeseries/commission-synthetic",
            json={
                "channels": [
                    {
                        "channel_key": "spp",
                        "name": "Standpipe Pressure",
                        "dimension": "pressure",
                        "unit": "psi",
                        "values": [4120.0, 4260.0],
                        "start": BASE_TS.isoformat(),
                        "step_seconds": 5.0,
                    }
                ],
            },
            headers={**headers("engineer"), "Idempotency-Key": "comm-001"},
        )
        assert comm_resp.status_code == 201, comm_resp.text
        comm_body = comm_resp.json()
        assert comm_body["report"]["alerts_raised"] == 1
        assert comm_body["report"]["totals"]["accepted"] == 2
        assert comm_body["health"]["points_accepted"] == 2

        frames = await _read_until(
            socket, lambda frame: frame["type"] == "event" and frame["event"]["type"] == "alert.raised"
        )
        assert any(f["type"] == "telemetry" for f in frames)

    from drillai.telemetry.outbox import decode_stream_cursor

    snap_resp = await client.get(
        f"{PREFIX}/wells/{well_id}/live/snapshot",
        headers=headers("viewer"),
    )
    assert snap_resp.status_code == 200, snap_resp.text
    snap = snap_resp.json()
    assert snap["well_id"] == well_id
    assert snap["telemetry_as_of"] is not None
    assert snap["alerts_as_of"] is not None
    decoded_cursor = decode_stream_cursor(snap["cursor"])
    assert decoded_cursor.well_id == well_id
    assert decoded_cursor.sequence == snap["stream_position"]
    assert len(snap["latest"]) == 1
    assert snap["latest"][0]["is_trustworthy"] is True
    assert len(snap["alerts"]) == 1
    assert "acknowledged" in snap["alerts"][0]["allowed_transitions"]


