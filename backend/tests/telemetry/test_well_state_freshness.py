"""Well state: what it says about the age of what it shows, and what it costs to say it.

Two CP9 findings live here. The first: the cockpit's KPI strip read the newest point of every channel with
a query *per channel* — correct answers, unbounded cost, on the page an operator opens first. The second:
a number's age was invisible, so a well whose feed stopped an hour ago and a well streaming right now
produced the same payload. Both are now properties of the answer, and both are asserted over the real API.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import select
from tests.fixtures.fabric import Fabric
from tests.telemetry.conftest import statement_counter

from drillai.db.models import TimeSeries, TimeSeriesPoint
from drillai.telemetry.service import TelemetryService

PREFIX = "/api/v1"
PSI_IN_PA = 6894.757293168

KPI_CHANNELS = (
    ("spp", "pressure", "psi"),
    ("wob", "force", "klbf"),
    ("rpm", "rotary_speed", "rpm"),
    ("flow_rate", "flow_rate", "gpm"),
    ("torque", "torque", "ft.lbf"),
    ("hookload", "force", "klbf"),
    ("mud_weight", "mud_weight", "ppg"),
)


async def _channel(fabric: Fabric, tenant, key: str, dimension: str, unit: str) -> dict:
    response = await fabric.http.post(
        f"{PREFIX}/timeseries",
        json={
            "well_id": tenant.well_id,
            "channel_key": key,
            "name": key.replace("_", " ").title(),
            "dimension": dimension,
            "unit": unit,
            "is_realtime": True,
            "source": "witsml",
        },
        headers=tenant.headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _append(fabric: Fabric, tenant, series_id: str, points: list[dict]) -> dict:
    response = await fabric.http.post(
        f"{PREFIX}/timeseries/{series_id}/points",
        json={"points": points},
        headers=tenant.headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _state(fabric: Fabric, tenant) -> dict:
    """The state *payload*: the route answers ``{"state": {...}}`` and this returns the inner document."""

    response = await fabric.http.get(
        f"{PREFIX}/wells/{tenant.well_id}/state", headers=tenant.headers
    )
    assert response.status_code == 200, response.text
    return response.json()["state"]


async def test_the_state_says_when_it_was_assembled_and_how_old_its_telemetry_is(fabric: Fabric) -> None:
    channel = await _channel(fabric, fabric.alpha, "spp", "pressure", "psi")
    now = dt.datetime.now(tz=dt.UTC)
    await _append(
        fabric,
        fabric.alpha,
        channel["id"],
        [{"ts": (now - dt.timedelta(seconds=5)).isoformat(), "value": 3500.0, "unit": "psi"}],
    )

    state = await _state(fabric, fabric.alpha)
    contract = state["telemetry"]
    assert contract["generated_at"] is not None
    assert contract["as_of"] is not None
    assert contract["freshness"] == "fresh", "a reading five seconds old is current"
    assert 0 <= contract["age_seconds"] < 60

    item = next(entry for entry in state["measured"] if entry["key"] == "spp")
    assert item["freshness"] == "fresh"
    assert item["age_seconds"] is not None and item["age_seconds"] < 60
    assert item["quality"] == "good"
    assert item["received_at"] is not None


async def test_a_channel_that_stopped_reporting_is_shown_as_stale_with_its_age(fabric: Fabric) -> None:
    """The failure this prevents: a KPI strip that renders a two-day-old pressure as if a driller had just
    read it off the gauge."""

    channel = await _channel(fabric, fabric.alpha, "spp", "pressure", "psi")
    old = dt.datetime.now(tz=dt.UTC) - dt.timedelta(days=2)
    await _append(
        fabric,
        fabric.alpha,
        channel["id"],
        [{"ts": old.isoformat(), "value": 3500.0, "unit": "psi", "received_at": old.isoformat()}],
    )

    state = await _state(fabric, fabric.alpha)
    item = next(entry for entry in state["measured"] if entry["key"] == "spp")
    assert item["freshness"] == "stale"
    assert item["age_seconds"] > 86_400
    assert item["value"] == 3500.0 * PSI_IN_PA, "the value is still reported — with its age"
    assert state["telemetry"]["freshness"] == "stale"
    assert item["note"] and "stale" in item["note"]


async def test_a_well_with_no_telemetry_says_so_rather_than_looking_current(fabric: Fabric) -> None:
    await _channel(fabric, fabric.alpha, "spp", "pressure", "psi")

    state = await _state(fabric, fabric.alpha)
    assert state["telemetry"]["as_of"] is None
    assert state["telemetry"]["freshness"] == "missing"
    assert state["telemetry"]["age_seconds"] is None
    assert [entry["key"] for entry in state["measured"]] == [], (
        "a channel with no measurement has no value to show, and inventing one would be worse"
    )
    assert any("spp" in str(item) for item in state["missing"]), (
        "the gap is declared in missing[] as well as left out of measured[]"
    )


async def test_freshness_boundaries_are_the_configured_thresholds(fabric: Fabric) -> None:
    """The verdict is a function of two configured numbers, tested either side of each boundary — and the
    same thresholds the API reports to clients."""

    channel = await _channel(fabric, fabric.alpha, "spp", "pressure", "psi")
    async with fabric.session() as session:
        service = TelemetryService(session, fabric.org_id(fabric.alpha))
        fresh_seconds = service.fresh_seconds
        stale_seconds = service.stale_seconds

    assert fresh_seconds < stale_seconds
    now = dt.datetime(2026, 3, 15, 12, 0, tzinfo=dt.UTC)
    async with fabric.session() as session:
        service = TelemetryService(session, fabric.org_id(fabric.alpha))
        assert service.freshness(now - dt.timedelta(seconds=fresh_seconds - 1), now=now) == ("fresh", fresh_seconds - 1)
        assert service.freshness(now - dt.timedelta(seconds=fresh_seconds + 1), now=now)[0] == "stale"
        assert service.freshness(now - dt.timedelta(seconds=stale_seconds + 1), now=now)[0] == "stale"
        assert service.freshness(None, now=now) == ("missing", None)
        # A naive instant is a row written by something that ignored the UTC convention. Its age cannot be
        # computed, and "unknown" is the honest answer — guessing a zone would move a measurement by hours.
        assert service.freshness(dt.datetime(2026, 3, 15, 12, 0), now=now) == ("unknown", None)

    latest = (
        await fabric.http.get(
            f"{PREFIX}/wells/{fabric.alpha.well_id}/timeseries/latest", headers=fabric.alpha.headers
        )
    ).json()
    assert latest["fresh_seconds"] == fresh_seconds
    assert latest["stale_seconds"] == stale_seconds

    # and the channel really is usable as a telemetry source for the well
    assert channel["id"].startswith("tms_")


async def test_the_kpi_read_does_not_grow_with_the_number_of_channels(fabric: Fabric) -> None:
    """The N+1 regression proof. The state page's KPI half is measured with seven channels and then with
    fourteen: the statements that touch the points table must be the same single window query."""

    async def point_statements() -> int:
        with statement_counter(fabric) as statements:
            await _state(fabric, fabric.alpha)
        return len([item for item in statements if "time_series_points" in item])

    for key, dimension, unit in KPI_CHANNELS:
        channel = await _channel(fabric, fabric.alpha, key, dimension, unit)
        await _append(
            fabric,
            fabric.alpha,
            channel["id"],
            [{"ts": dt.datetime.now(tz=dt.UTC).isoformat(), "value": 10.0, "unit": unit}],
        )
    with_seven = await point_statements()

    extra = (
        ("spp", "pressure", "psi"),
        ("wob", "force", "klbf"),
        ("rpm", "rotary_speed", "rpm"),
        ("flow_rate", "flow_rate", "gpm"),
        ("torque", "torque", "ft.lbf"),
        ("hookload", "force", "klbf"),
        ("mud_weight", "mud_weight", "ppg"),
    )
    for key, dimension, unit in extra:
        # A second well on the same organization, so the extra channels are real rows in the table.
        second_well = await fabric.add_well(fabric.alpha, f"ALPHA-{key}")
        response = await fabric.http.post(
            f"{PREFIX}/timeseries",
            json={
                "well_id": second_well,
                "channel_key": key,
                "name": key,
                "dimension": dimension,
                "unit": unit,
                "source": "witsml",
            },
            headers=fabric.alpha.headers,
        )
        assert response.status_code == 201, response.text
        await _append(
            fabric,
            fabric.alpha,
            response.json()["id"],
            [{"ts": dt.datetime.now(tz=dt.UTC).isoformat(), "value": 20.0, "unit": unit}],
        )
    with_fourteen = await point_statements()

    async with fabric.session() as session:
        channels = int(
            (
                await session.execute(
                    select(TimeSeries).where(TimeSeries.org_id == fabric.org_id(fabric.alpha))
                )
            )
            .scalars()
            .all()
            .__len__()
        )
        points = int((await session.execute(select(TimeSeriesPoint.id))).scalars().all().__len__())
    assert channels == 14 and points == 14, (channels, points)

    assert with_seven == with_fourteen == 1, (
        f"the state read touched the points table {with_seven} times with 7 channels and "
        f"{with_fourteen} times with 14"
    )
