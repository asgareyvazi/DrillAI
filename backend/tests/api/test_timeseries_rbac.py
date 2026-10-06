"""Telemetry authorisation over HTTP, with a catalogued principal on every request.

The model-level matrix lives in ``tests/telemetry/test_rbac_telemetry.py``. What is asserted here is that
the routes *consult* the model: the same request that lands as an engineer is refused as a viewer, a
refused write leaves nothing behind, and the read surfaces a monitor needs are readable by every role
that may look at the well.

These run against the auth-disabled development identity (``X-Dev-Roles``), which is why they live in the
API suite — the tenant fabric's bearer tokens are the other half of the story, and they are exercised in
``tests/telemetry/``.
"""

from __future__ import annotations

import datetime as dt

from tests.api.conftest import headers

PREFIX = "/api/v1"
BASE_TS = dt.datetime(2026, 3, 15, 8, 0, tzinfo=dt.UTC)


# --------------------------------------------------------------------------- the routes


async def _channel(
    client,
    *,
    channel_key: str = "spp",
    name: str = "Standpipe pressure",
    role: str = "engineer",
    tree_role: str = "well_manager",
) -> dict:
    """project → well → channel, through the API.

    The tree is built as ``tree_role`` because creating a project and a well are different
    entitlements (``project.write``, ``well.create``) that a viewer deliberately does not hold;
    ``role`` is the identity the telemetry calls are made as, which is the thing each test is about.
    """

    project = await client.post(
        f"{PREFIX}/projects", json={"name": f"RBAC {channel_key}"}, headers=headers(tree_role)
    )
    assert project.status_code in {200, 201}, project.text
    well = await client.post(
        f"{PREFIX}/wells",
        json={"project_id": project.json()["id"], "name": f"W-{channel_key}", "well_type": "development_producer"},
        headers=headers(tree_role),
    )
    assert well.status_code == 201, well.text
    channel = await client.post(
        f"{PREFIX}/timeseries",
        json={
            "well_id": well.json()["id"],
            "channel_key": channel_key,
            "name": name,
            "dimension": "pressure",
            "unit": "psi",
            "source": "witsml",
        },
        headers=headers(role),
    )
    assert channel.status_code == 201, channel.text
    return channel.json()


async def test_a_viewer_cannot_append_a_batch_but_an_engineer_can(client) -> None:
    """The catalogue is real because a route consults it: the identical request that lands as an engineer
    is refused as a viewer, with the permission named and nothing written."""

    channel = await _channel(client)
    batch = {"points": [{"ts": BASE_TS.isoformat(), "value": 3500.0, "unit": "psi"}]}

    refused = await client.post(
        f"{PREFIX}/timeseries/{channel['id']}/points", json=batch, headers=headers("viewer")
    )
    assert refused.status_code == 403, refused.text
    assert refused.json()["error"]["code"] == "security.permission_denied"

    still_empty = await client.get(
        f"{PREFIX}/timeseries/{channel['id']}/points", headers=headers("viewer")
    )
    assert still_empty.json()["total"] == 0, "a refused append must not have written anything"

    allowed = await client.post(
        f"{PREFIX}/timeseries/{channel['id']}/points", json=batch, headers=headers("engineer")
    )
    assert allowed.status_code == 201, allowed.text
    assert allowed.json()["accepted"] == 1


async def test_a_viewer_cannot_register_a_channel_but_can_read_the_monitor(client) -> None:
    channel = await _channel(client)
    await client.post(
        f"{PREFIX}/timeseries/{channel['id']}/points",
        json={"points": [{"ts": BASE_TS.isoformat(), "value": 3500.0, "unit": "psi"}]},
        headers=headers("engineer"),
    )

    refusal = await client.post(
        f"{PREFIX}/timeseries",
        json={
            "well_id": channel["well_id"],
            "channel_key": "wob",
            "name": "Weight on bit",
            "dimension": "force",
            "unit": "klbf",
        },
        headers=headers("viewer"),
    )
    assert refusal.status_code == 403, refusal.text

    monitor = await client.get(
        f"{PREFIX}/wells/{channel['well_id']}/timeseries/latest", headers=headers("viewer")
    )
    assert monitor.status_code == 200, monitor.text
    assert monitor.json()["total"] == 1
    assert monitor.json()["items"][0]["unit"] == "Pa"

    channels = await client.get(
        f"{PREFIX}/timeseries", params={"well_id": channel["well_id"]}, headers=headers("viewer")
    )
    assert channels.status_code == 200, channels.text
    assert channels.json()["total"] == 1

    points = await client.get(
        f"{PREFIX}/timeseries/{channel['id']}/points", headers=headers("viewer")
    )
    assert points.status_code == 200, points.text
    assert points.json()["returned"] == 1


async def test_a_caller_with_no_usable_role_is_refused_everywhere(client) -> None:
    unknown = {"X-Dev-Roles": "not_a_real_role"}
    assert (await client.get(f"{PREFIX}/timeseries", headers=unknown)).status_code == 403
    assert (
        await client.post(
            f"{PREFIX}/timeseries", json={}, headers=unknown
        )
    ).status_code == 403
    assert (
        await client.get(f"{PREFIX}/wells/wel_1/timeseries/latest", headers=unknown)
    ).status_code == 403


async def test_a_known_role_beside_an_unknown_one_is_still_a_usable_identity(client) -> None:
    """The development identity path must not degrade into "no roles means everything", and it must not
    refuse a caller who named one real role among typos."""

    response = await client.get(
        f"{PREFIX}/timeseries", headers={"X-Dev-Roles": "viewer,not_a_real_role"}
    )
    assert response.status_code == 200, response.text


async def test_an_auditor_may_read_telemetry_and_may_not_write_it(client) -> None:
    """The auditor's whole purpose is to see without touching."""

    channel = await _channel(client)
    read = await client.get(f"{PREFIX}/timeseries/{channel['id']}", headers=headers("auditor"))
    assert read.status_code == 200, read.text
    write = await client.post(
        f"{PREFIX}/timeseries/{channel['id']}/points",
        json={"points": [{"ts": BASE_TS.isoformat(), "value": 1.0, "unit": "psi"}]},
        headers=headers("auditor"),
    )
    assert write.status_code == 403, write.text


async def test_the_register_survives_the_permission_check(client) -> None:
    """A refused write leaves no channel behind — the check happens before the mutation, not after."""

    project = await client.post(f"{PREFIX}/projects", json={"name": "Refusals"}, headers=headers("well_manager"))
    well = await client.post(
        f"{PREFIX}/wells",
        json={"project_id": project.json()["id"], "name": "W-refuse", "well_type": "development_producer"},
        headers=headers("well_manager"),
    )
    refusal = await client.post(
        f"{PREFIX}/timeseries",
        json={
            "well_id": well.json()["id"],
            "channel_key": "spp",
            "name": "Standpipe pressure",
            "dimension": "pressure",
            "unit": "psi",
        },
        headers=headers("viewer"),
    )
    assert refusal.status_code == 403, refusal.text
    listing = await client.get(
        f"{PREFIX}/timeseries", params={"well_id": well.json()["id"]}, headers=headers("viewer")
    )
    assert listing.json()["total"] == 0
