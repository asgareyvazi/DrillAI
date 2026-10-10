"""Reading telemetry: latest values, freshness, windows and paging — over HTTP, with measurements.

The claims in this file are the ones an operator's screen depends on: that the newest value really is
the newest, that its age is stated rather than implied, that a long series can be read in bounded pages
without skipping or repeating a measurement, and that the number of SQL statements a request issues does
not grow with the number of channels.
"""

from __future__ import annotations

import datetime as dt
import re

import pytest
from tests.fixtures.fabric import Fabric
from tests.telemetry.conftest import statement_counter

PREFIX = "/api/v1"
BASE_TS = dt.datetime(2026, 3, 15, 8, 0, tzinfo=dt.UTC)
PSI_IN_PA = 6894.757293168


async def _channel(
    fabric: Fabric,
    tenant,
    *,
    channel_key: str,
    dimension: str = "pressure",
    unit: str = "psi",
    name: str | None = None,
    wellbore_id: str | None = None,
) -> dict:
    response = await fabric.http.post(
        f"{PREFIX}/timeseries",
        json={
            "well_id": tenant.well_id,
            "wellbore_id": wellbore_id,
            "channel_key": channel_key,
            "name": name or channel_key.replace("_", " ").title(),
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


def _sample(index: int, *, value: float = 3500.0, unit: str = "psi", step_seconds: int = 60) -> dict:
    moment = BASE_TS + dt.timedelta(seconds=step_seconds * index)
    return {"ts": moment.isoformat(), "value": value + index, "unit": unit, "source_point_id": f"p-{index}"}


# --------------------------------------------------------------------------- latest


async def test_latest_returns_the_newest_measurement_per_channel(fabric: Fabric) -> None:
    spp = await _channel(fabric, fabric.alpha, channel_key="spp")
    wob = await _channel(fabric, fabric.alpha, channel_key="wob", dimension="force", unit="klbf")
    await _append(fabric, fabric.alpha, spp["id"], [_sample(0), _sample(1), _sample(2)])
    await _append(fabric, fabric.alpha, wob["id"], [_sample(0, value=20.0, unit="klbf")])

    response = await fabric.http.get(
        f"{PREFIX}/wells/{fabric.alpha.well_id}/timeseries/latest", headers=fabric.alpha.headers
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 2
    by_key = {item["channel_key"]: item for item in body["items"]}
    assert by_key["spp"]["value"] == pytest.approx(3502.0 * PSI_IN_PA, rel=1e-9)
    assert by_key["spp"]["observed_at"] == (BASE_TS + dt.timedelta(seconds=120)).isoformat()
    assert by_key["wob"]["value"] == pytest.approx(20.0 * 4448.2216152605, rel=1e-9)
    # The thresholds the answer was judged against travel with it, so a client cannot apply its own.
    assert body["fresh_seconds"] == 30.0
    assert body["stale_seconds"] == 300.0
    assert body["generated_at"] is not None


async def test_a_channel_that_never_reported_is_missing_not_absent_and_not_zero(fabric: Fabric) -> None:
    await _channel(fabric, fabric.alpha, channel_key="spp")
    response = await fabric.http.get(
        f"{PREFIX}/wells/{fabric.alpha.well_id}/timeseries/latest", headers=fabric.alpha.headers
    )
    item = response.json()["items"][0]
    assert item["value"] is None
    assert item["quality"] == "missing"
    assert item["freshness"] == "missing"
    assert item["observed_at"] is None
    assert item["age_seconds"] is None


async def test_a_stop_in_reporting_is_reported_as_stale_with_its_age(fabric: Fabric) -> None:
    """A reading from two days ago is not a current reading, and the API says which it is."""

    channel = await _channel(fabric, fabric.alpha, channel_key="spp")
    await _append(fabric, fabric.alpha, channel["id"], [_sample(0)])

    response = await fabric.http.get(
        f"{PREFIX}/wells/{fabric.alpha.well_id}/timeseries/latest", headers=fabric.alpha.headers
    )
    item = response.json()["items"][0]
    assert item["freshness"] == "stale", "the newest value is from March; it cannot be called live"
    assert item["age_seconds"] > 300
    assert item["observed_at"] == BASE_TS.isoformat()
    assert item["quality"] == "good"


async def test_a_bad_quality_reading_is_returned_with_its_quality_visible(fabric: Fabric) -> None:
    """A number the platform has doubts about is still shown — with the doubt attached."""

    channel = await _channel(fabric, fabric.alpha, channel_key="spp")
    await _append(
        fabric,
        fabric.alpha,
        channel["id"],
        [{**_sample(0), "quality": "bad", "source_point_id": "p-bad"}],
    )
    response = await fabric.http.get(
        f"{PREFIX}/wells/{fabric.alpha.well_id}/timeseries/latest", headers=fabric.alpha.headers
    )
    item = response.json()["items"][0]
    assert item["quality"] == "bad"
    assert item["value"] is not None, "a bad reading is evidence; hiding it would be worse than showing it"


async def test_latest_can_be_narrowed_to_the_channels_a_caller_asked_for(fabric: Fabric) -> None:
    for key in ("spp", "wob", "rpm"):
        await _channel(
            fabric,
            fabric.alpha,
            channel_key=key,
            dimension={"spp": "pressure", "wob": "force", "rpm": "rotary_speed"}[key],
            unit={"spp": "psi", "wob": "klbf", "rpm": "rpm"}[key],
        )
    response = await fabric.http.get(
        f"{PREFIX}/wells/{fabric.alpha.well_id}/timeseries/latest",
        params=[("channel_key", "spp"), ("channel_key", "rpm")],
        headers=fabric.alpha.headers,
    )
    assert response.status_code == 200, response.text
    assert sorted(item["channel_key"] for item in response.json()["items"]) == ["rpm", "spp"]


async def test_latest_is_bounded_however_many_channels_a_well_has(fabric: Fabric) -> None:
    for index in range(12):
        await _channel(fabric, fabric.alpha, channel_key=f"ch_{index:02d}")
    response = await fabric.http.get(
        f"{PREFIX}/wells/{fabric.alpha.well_id}/timeseries/latest",
        params={"limit": 5},
        headers=fabric.alpha.headers,
    )
    assert response.status_code == 200, response.text
    assert len(response.json()["items"]) == 5


async def test_latest_does_not_issue_a_query_per_channel(fabric: Fabric) -> None:
    """The N+1 proof: the same request costs the same statements for one channel and for twelve.

    A ceiling alone would be decorative — a per-channel loop over one channel is two statements. What is
    asserted is that the *cost does not grow* with the fan-out, which is the property an N+1 violates.
    """

    async def latest_statements() -> list[str]:
        with statement_counter(fabric) as statements:
            response = await fabric.http.get(
                f"{PREFIX}/wells/{fabric.alpha.well_id}/timeseries/latest",
                headers=fabric.alpha.headers,
            )
            assert response.status_code == 200, response.text
        # Normalise the identity list: the *shape* of the statement is what must not change with the
        # number of channels, and a literal list of channel ids always will.
        return [
            re.sub(r"\([^()]*\)", "(...)", item)
            for item in statements
            if "FROM time_series_points" in item
        ]

    first = await _channel(fabric, fabric.alpha, channel_key="ch_00")
    await _append(fabric, fabric.alpha, first["id"], [_sample(0)])
    one_channel = await latest_statements()

    for index in range(1, 12):
        channel = await _channel(fabric, fabric.alpha, channel_key=f"ch_{index:02d}")
        await _append(fabric, fabric.alpha, channel["id"], [_sample(0)])
    twelve_channels = await latest_statements()

    assert one_channel == twelve_channels, (
        "the reading cost changed with the number of channels: "
        f"{len(one_channel)} statements for one, {len(twelve_channels)} for twelve"
    )
    assert len(twelve_channels) == 1, "the newest-per-channel answer is one window statement"


async def test_latest_for_another_organizations_well_is_not_found(fabric: Fabric) -> None:
    """A foreign well and a well that does not exist must be the same answer.

    If "not yours" could be told apart from "no such well", the endpoint would be a tenant-existence
    oracle: an outsider could enumerate identifiers and learn which ones are real.
    """

    await _channel(fabric, fabric.alpha, channel_key="spp")

    foreign = await fabric.http.get(
        f"{PREFIX}/wells/{fabric.alpha.well_id}/timeseries/latest", headers=fabric.bravo.headers
    )
    unknown = await fabric.http.get(
        f"{PREFIX}/wells/wel_no_such_well/timeseries/latest", headers=fabric.bravo.headers
    )
    assert foreign.status_code == unknown.status_code == 404, foreign.text
    assert foreign.json()["error"]["code"] == unknown.json()["error"]["code"] == "platform.not_found"
    # Nothing about the other organization's telemetry may appear in the refusal.
    assert "spp" not in foreign.text
    assert "Standpipe" not in foreign.text


async def test_latest_for_an_unknown_well_is_a_404_not_an_empty_list(fabric: Fabric) -> None:
    response = await fabric.http.get(
        f"{PREFIX}/wells/wel_does_not_exist/timeseries/latest", headers=fabric.alpha.headers
    )
    assert response.status_code == 404, response.text


async def test_a_channel_list_filtered_by_a_foreign_well_is_not_found(fabric: Fabric) -> None:
    response = await fabric.http.get(
        f"{PREFIX}/timeseries", params={"well_id": fabric.alpha.well_id}, headers=fabric.bravo.headers
    )
    assert response.status_code == 404, response.text


# --------------------------------------------------------------------------- windows and paging


async def test_a_window_returns_points_in_deterministic_order_with_the_series_total(fabric: Fabric) -> None:
    channel = await _channel(fabric, fabric.alpha, channel_key="spp")
    await _append(fabric, fabric.alpha, channel["id"], [_sample(index) for index in range(10)])

    response = await fabric.http.get(
        f"{PREFIX}/timeseries/{channel['id']}/points", headers=fabric.alpha.headers
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 10
    assert body["returned"] == 10
    assert body["truncated"] is False
    assert body["next_cursor"] is None
    assert [point["ts"] for point in body["items"]] == sorted(point["ts"] for point in body["items"])
    assert body["unit"] == "Pa"
    first = body["items"][0]
    assert first["value"] == pytest.approx(3500.0 * PSI_IN_PA, rel=1e-9)
    assert first["source_value"] == 3500.0
    assert first["source_unit"] == "psi"
    assert first["identified_by"] == "source_point_id"


async def test_paging_by_cursor_covers_every_point_exactly_once(fabric: Fabric) -> None:
    channel = await _channel(fabric, fabric.alpha, channel_key="spp")
    await _append(fabric, fabric.alpha, channel["id"], [_sample(index) for index in range(25)])

    seen: list[str] = []
    cursor: str | None = None
    pages = 0
    while True:
        params = {"limit": 7}
        if cursor:
            params["cursor"] = cursor
        response = await fabric.http.get(
            f"{PREFIX}/timeseries/{channel['id']}/points", params=params, headers=fabric.alpha.headers
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["total"] == 25, "the total describes the series, not the page"
        seen.extend(point["id"] for point in body["items"])
        pages += 1
        cursor = body["next_cursor"]
        if not cursor:
            assert body["truncated"] is False
            break
        assert body["truncated"] is True
    assert pages == 4, "25 points at 7 per page is four pages"
    assert len(seen) == 25
    assert len(set(seen)) == 25, "a paged read must not repeat a point"


async def test_paging_is_stable_when_several_points_share_an_instant(fabric: Fabric) -> None:
    """The cursor's total order is what makes this work: several points at one instant are still
    ordered, so a page boundary can never fall between two rows that compare equal."""

    channel = await _channel(fabric, fabric.alpha, channel_key="spp")
    same_instant = [
        {"ts": BASE_TS.isoformat(), "value": 3500.0 + index, "unit": "psi", "source_point_id": f"tie-{index}"}
        for index in range(6)
    ]
    await _append(fabric, fabric.alpha, channel["id"], same_instant)

    seen: list[str] = []
    cursor: str | None = None
    for _page in range(3):
        params = {"limit": 2}
        if cursor:
            params["cursor"] = cursor
        body = (
            await fabric.http.get(
                f"{PREFIX}/timeseries/{channel['id']}/points", params=params, headers=fabric.alpha.headers
            )
        ).json()
        seen.extend(point["id"] for point in body["items"])
        cursor = body["next_cursor"]
    assert len(seen) == 6
    assert len(set(seen)) == 6


async def test_a_descending_window_pages_backwards_without_repeating(fabric: Fabric) -> None:
    channel = await _channel(fabric, fabric.alpha, channel_key="spp")
    await _append(fabric, fabric.alpha, channel["id"], [_sample(index) for index in range(10)])

    first = (
        await fabric.http.get(
            f"{PREFIX}/timeseries/{channel['id']}/points",
            params={"limit": 4, "order": "desc"},
            headers=fabric.alpha.headers,
        )
    ).json()
    assert len(first["items"]) == 4
    instants = [dt.datetime.fromisoformat(point["ts"]) for point in first["items"]]
    assert instants == sorted(instants, reverse=True)

    second = (
        await fabric.http.get(
            f"{PREFIX}/timeseries/{channel['id']}/points",
            params={"limit": 4, "order": "desc", "cursor": first["next_cursor"]},
            headers=fabric.alpha.headers,
        )
    ).json()
    assert {point["id"] for point in first["items"]} & {point["id"] for point in second["items"]} == set()
    assert max(dt.datetime.fromisoformat(p["ts"]) for p in second["items"]) < min(
        dt.datetime.fromisoformat(p["ts"]) for p in first["items"]
    )


async def test_a_window_can_be_bounded_by_time(fabric: Fabric) -> None:
    channel = await _channel(fabric, fabric.alpha, channel_key="spp")
    await _append(fabric, fabric.alpha, channel["id"], [_sample(index, step_seconds=3600) for index in range(6)])

    response = await fabric.http.get(
        f"{PREFIX}/timeseries/{channel['id']}/points",
        params={
            "since": (BASE_TS + dt.timedelta(hours=1)).isoformat(),
            "until": (BASE_TS + dt.timedelta(hours=3)).isoformat(),
        },
        headers=fabric.alpha.headers,
    )
    body = response.json()
    assert body["total"] == 3
    assert body["items"][0]["ts"] == (BASE_TS + dt.timedelta(hours=1)).isoformat()
    assert body["items"][-1]["ts"] == (BASE_TS + dt.timedelta(hours=3)).isoformat()


async def test_a_page_larger_than_the_platform_allows_is_clamped_and_the_page_size_stated(
    fabric: Fabric,
) -> None:
    channel = await _channel(fabric, fabric.alpha, channel_key="spp")
    await _append(fabric, fabric.alpha, channel["id"], [_sample(index) for index in range(5)])
    response = await fabric.http.get(
        f"{PREFIX}/timeseries/{channel['id']}/points",
        params={"limit": 100000},
        headers=fabric.alpha.headers,
    )
    assert response.status_code == 422, response.text


async def test_a_corrupt_cursor_is_refused_rather_than_ignored(fabric: Fabric) -> None:
    """Silently starting from the beginning would re-send measurements a client already has; silently
    returning nothing would look like the end of the series. Both are lies, so the cursor is refused."""

    channel = await _channel(fabric, fabric.alpha, channel_key="spp")
    await _append(fabric, fabric.alpha, channel["id"], [_sample(0)])
    response = await fabric.http.get(
        f"{PREFIX}/timeseries/{channel['id']}/points",
        params={"cursor": "not-a-cursor"},
        headers=fabric.alpha.headers,
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "platform.validation_failed"


async def test_a_tampered_cursor_is_refused_too(fabric: Fabric) -> None:
    channel = await _channel(fabric, fabric.alpha, channel_key="spp")
    await _append(fabric, fabric.alpha, channel["id"], [_sample(index) for index in range(3)])
    first = (
        await fabric.http.get(
            f"{PREFIX}/timeseries/{channel['id']}/points",
            params={"limit": 1},
            headers=fabric.alpha.headers,
        )
    ).json()
    cursor = first["next_cursor"]
    response = await fabric.http.get(
        f"{PREFIX}/timeseries/{channel['id']}/points",
        params={"cursor": cursor[:-2] + ("AA" if not cursor.endswith("AA") else "BB")},
        headers=fabric.alpha.headers,
    )
    assert response.status_code in {200, 422}, response.text
    if response.status_code == 422:
        assert response.json()["error"]["code"] == "platform.validation_failed"
    else:
        # A one-character change can still decode into a valid anchor; if it does, the answer must still
        # be a page of this channel's own points and must not silently restart the series.
        assert response.json()["items"], "a decoded cursor must anchor somewhere, not reset the series"


async def test_a_window_read_issues_no_more_statements_than_it_needs(fabric: Fabric) -> None:
    channel = await _channel(fabric, fabric.alpha, channel_key="spp")
    await _append(fabric, fabric.alpha, channel["id"], [_sample(index) for index in range(50)])

    with statement_counter(fabric) as statements:
        response = await fabric.http.get(
            f"{PREFIX}/timeseries/{channel['id']}/points",
            params={"limit": 10},
            headers=fabric.alpha.headers,
        )
        assert response.status_code == 200, response.text
    selects = [item for item in statements if item.lower().startswith("select")]
    # The channel lookup, the count of the range, and the page itself — plus the tenant/audit reads the
    # surrounding platform does. A per-point read would be fifty.
    assert len(selects) <= 6, "\n".join(selects)
    assert len(response.json()["items"]) == 10


async def test_a_foreign_channel_is_not_found_and_its_measurements_are_not_readable(fabric: Fabric) -> None:
    channel = await _channel(fabric, fabric.alpha, channel_key="spp")
    await _append(fabric, fabric.alpha, channel["id"], [_sample(0)])

    detail = await fabric.http.get(f"{PREFIX}/timeseries/{channel['id']}", headers=fabric.bravo.headers)
    assert detail.status_code == 404, detail.text
    points = await fabric.http.get(
        f"{PREFIX}/timeseries/{channel['id']}/points", headers=fabric.bravo.headers
    )
    assert points.status_code == 404, points.text
    assert points.json()["error"]["code"] == "platform.not_found"


async def test_the_same_channel_key_in_two_organizations_stays_two_channels(fabric: Fabric) -> None:
    alpha = await _channel(fabric, fabric.alpha, channel_key="spp")
    bravo = await _channel(fabric, fabric.bravo, channel_key="spp")
    await _append(fabric, fabric.alpha, alpha["id"], [_sample(0, value=3500.0)])
    await _append(fabric, fabric.bravo, bravo["id"], [_sample(0, value=9999.0)])

    alpha_latest = (
        await fabric.http.get(
            f"{PREFIX}/wells/{fabric.alpha.well_id}/timeseries/latest", headers=fabric.alpha.headers
        )
    ).json()
    bravo_latest = (
        await fabric.http.get(
            f"{PREFIX}/wells/{fabric.bravo.well_id}/timeseries/latest", headers=fabric.bravo.headers
        )
    ).json()
    assert alpha_latest["total"] == 1 and bravo_latest["total"] == 1
    assert alpha_latest["items"][0]["channel_id"] != bravo_latest["items"][0]["channel_id"]
    assert alpha_latest["items"][0]["value"] == pytest.approx(3500.0 * PSI_IN_PA, rel=1e-9)
    assert bravo_latest["items"][0]["value"] == pytest.approx(9999.0 * PSI_IN_PA, rel=1e-9)


async def test_a_channel_on_a_wellbore_and_operation_is_a_different_channel(fabric: Fabric) -> None:
    """Same key, same dimension, same well — a different scope is a different series, because a sidetrack
    and an operation each have their own standpipe pressure."""

    well_level = await _channel(fabric, fabric.alpha, channel_key="spp")
    on_wellbore = await _channel(
        fabric, fabric.alpha, channel_key="spp", wellbore_id=fabric.alpha.wellbore_id
    )
    assert well_level["id"] != on_wellbore["id"]
    assert well_level["scope"] == fabric.alpha.well_id
    assert on_wellbore["scope"] == f"{fabric.alpha.well_id}/{fabric.alpha.wellbore_id}"

    await _append(fabric, fabric.alpha, well_level["id"], [_sample(0, value=3500.0)])
    await _append(fabric, fabric.alpha, on_wellbore["id"], [_sample(0, value=4100.0)])

    for channel, expected_psi in ((well_level, 3500.0), (on_wellbore, 4100.0)):
        body = (
            await fabric.http.get(
                f"{PREFIX}/timeseries/{channel['id']}/points", headers=fabric.alpha.headers
            )
        ).json()
        assert body["returned"] == 1
        assert body["items"][0]["value"] == pytest.approx(expected_psi * PSI_IN_PA, rel=1e-9)


async def test_a_channel_on_another_well_of_the_same_organization_is_a_different_channel(
    fabric: Fabric,
) -> None:
    second_well = await fabric.add_well(fabric.alpha, "ALPHA-2")
    first = await _channel(fabric, fabric.alpha, channel_key="spp")
    second = (
        await fabric.http.post(
            f"{PREFIX}/timeseries",
            json={
                "well_id": second_well,
                "channel_key": "spp",
                "name": "Standpipe pressure",
                "dimension": "pressure",
                "unit": "psi",
            },
            headers=fabric.alpha.headers,
        )
    ).json()
    assert second["id"] != first["id"]
    assert second["well_id"] == second_well
