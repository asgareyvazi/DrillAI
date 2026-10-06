"""Parallel ingestion: what two workers doing the same thing produce.

Every test here fires *real* concurrent requests at the real application over the real database and then
checks rows. The property under test is not "this usually works" but "the database decides": a replay
cannot become a second row, a conflict cannot become a silent overwrite, and a lost race surfaces as a
counted duplicate rather than as a swallowed exception or a 500.
"""

from __future__ import annotations

import asyncio
import datetime as dt

import pytest
from sqlalchemy import func, select
from tests.fixtures.fabric import Fabric

from drillai.db.models import TimeSeries, TimeSeriesPoint

PREFIX = "/api/v1"
BASE_TS = dt.datetime(2026, 3, 15, 8, 0, tzinfo=dt.UTC)
PSI_IN_PA = 6894.757293168


async def _channel(fabric: Fabric, tenant, *, channel_key: str = "spp", unit: str = "psi") -> dict:
    response = await fabric.http.post(
        f"{PREFIX}/timeseries",
        json={
            "well_id": tenant.well_id,
            "channel_key": channel_key,
            "name": channel_key.replace("_", " ").title(),
            "dimension": "pressure",
            "unit": unit,
            "source": "witsml",
        },
        headers=tenant.headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


def _batch(count: int, *, prefix: str = "p", value: float = 3500.0, offset: int = 0) -> list[dict]:
    return [
        {
            "ts": (BASE_TS + dt.timedelta(seconds=60 * (offset + index))).isoformat(),
            "value": value + index,
            "unit": "psi",
            "source_point_id": f"{prefix}-{offset + index}",
        }
        for index in range(count)
    ]


async def _append(fabric: Fabric, tenant, series_id: str, points: list[dict], **extra):
    return await fabric.http.post(
        f"{PREFIX}/timeseries/{series_id}/points",
        json={"points": points, **extra},
        headers=tenant.headers,
    )


async def _row_count(fabric: Fabric, series_id: str) -> int:
    async with fabric.session() as session:
        return int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(TimeSeriesPoint)
                    .where(TimeSeriesPoint.series_id == series_id)
                )
            ).scalar_one()
        )


async def _channel_count(fabric: Fabric, tenant, channel_key: str) -> int:
    async with fabric.session() as session:
        return int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(TimeSeries)
                    .where(
                        TimeSeries.org_id == fabric.org_id(tenant),
                        TimeSeries.channel_key == channel_key,
                    )
                )
            ).scalar_one()
        )


async def test_two_concurrent_creations_of_the_same_channel_make_one_channel(fabric: Fabric) -> None:
    """The unique index decides; both callers get an answer and exactly one of them made the row."""

    async def create():
        return await fabric.http.post(
            f"{PREFIX}/timeseries",
            json={
                "well_id": fabric.alpha.well_id,
                "channel_key": "spp",
                "name": "Standpipe pressure",
                "dimension": "pressure",
                "unit": "psi",
            },
            headers=fabric.alpha.headers,
        )

    first, second = await asyncio.gather(create(), create())
    assert first.status_code == second.status_code == 201, (first.text, second.text)
    assert first.json()["id"] == second.json()["id"], "two ids for one identity is two channels"
    assert sorted([first.json()["created"], second.json()["created"]]) == [False, True]
    assert await _channel_count(fabric, fabric.alpha, "spp") == 1


async def test_two_concurrent_appends_of_the_same_batch_store_it_once(fabric: Fabric) -> None:
    """The replay guarantee under a race: the same twenty measurements, sent twice at once, are twenty
    rows — and both callers are told, in the reconciled counts, what happened to theirs."""

    channel = await _channel(fabric, fabric.alpha)
    points = _batch(20)

    first, second = await asyncio.gather(
        _append(fabric, fabric.alpha, channel["id"], points),
        _append(fabric, fabric.alpha, channel["id"], points),
    )
    assert first.status_code == second.status_code == 201, (first.text, second.text)
    reports = [first.json(), second.json()]
    assert sum(report["accepted"] for report in reports) == 20
    assert sum(report["duplicates"] for report in reports) == 20
    for report in reports:
        assert report["reconciled"] is True
    assert await _row_count(fabric, channel["id"]) == 20


async def test_two_concurrent_appends_of_different_values_never_overwrite_silently(fabric: Fabric) -> None:
    """Two sources disagreeing about the same moment: the first value stands, the second is reported as a
    conflict, and the row still holds what it held."""

    channel = await _channel(fabric, fabric.alpha)
    early = _batch(8, prefix="p", value=3500.0)
    late = _batch(8, prefix="p", value=4000.0)

    first, second = await asyncio.gather(
        _append(fabric, fabric.alpha, channel["id"], early),
        _append(fabric, fabric.alpha, channel["id"], late),
    )
    assert first.status_code == second.status_code == 201, (first.text, second.text)
    reports = [first.json(), second.json()]
    assert sum(report["accepted"] for report in reports) == 8, "each identity landed exactly once"
    assert sum(report["rejected"] for report in reports) == 8
    assert sum(len(report["conflicts"]) for report in reports) == 8
    assert await _row_count(fabric, channel["id"]) == 8

    async with fabric.session() as session:
        values = set(
            (
                await session.execute(
                    select(TimeSeriesPoint.value).where(TimeSeriesPoint.series_id == channel["id"])
                )
            )
            .scalars()
            .all()
        )
    expected = {round((3500.0 + index) * PSI_IN_PA, 6) for index in range(8)}
    assert {round(value, 6) for value in values} == expected, (
        "the stored values are one coherent set, not a mixture of the two batches"
    )


async def test_the_same_identity_twice_in_one_batch_is_a_conflict_not_two_rows(fabric: Fabric) -> None:
    """A single caller can replay itself. The second occurrence is classified exactly as an external
    replay would be — the identity rule does not depend on who is asking."""

    channel = await _channel(fabric, fabric.alpha)
    points = [
        {"ts": BASE_TS.isoformat(), "value": 3500.0, "unit": "psi", "source_point_id": "dup"},
        {"ts": BASE_TS.isoformat(), "value": 3600.0, "unit": "psi", "source_point_id": "dup"},
    ]
    report = (await _append(fabric, fabric.alpha, channel["id"], points)).json()
    assert report["received"] == 2
    assert report["accepted"] == 1
    assert report["rejected"] == 1
    assert report["reconciled"] is True
    assert await _row_count(fabric, channel["id"]) == 1


async def test_a_duplicate_inside_one_batch_with_the_same_value_is_counted_as_a_duplicate(
    fabric: Fabric,
) -> None:
    channel = await _channel(fabric, fabric.alpha)
    point = {"ts": BASE_TS.isoformat(), "value": 3500.0, "unit": "psi", "source_point_id": "same"}
    report = (await _append(fabric, fabric.alpha, channel["id"], [point, dict(point)])).json()
    assert report["accepted"] == 1
    assert report["duplicates"] == 1
    assert report["reconciled"] is True
    assert await _row_count(fabric, channel["id"]) == 1


async def test_two_parallel_retries_with_one_idempotency_key_answer_identically(fabric: Fabric) -> None:
    """A client that retried because it did not see the first answer must not be able to store the batch
    twice — and must get the same answer, not a second, differently-counted one."""

    channel = await _channel(fabric, fabric.alpha)
    points = _batch(5)
    headers = {**fabric.alpha.headers, "Idempotency-Key": "retry-0001"}

    async def send():
        return await fabric.http.post(
            f"{PREFIX}/timeseries/{channel['id']}/points", json={"points": points}, headers=headers
        )

    first, second = await asyncio.gather(send(), send())
    assert first.status_code == second.status_code == 201, (first.text, second.text)
    assert first.json() == second.json(), "a replay must be the stored answer, not a recomputation"
    assert await _row_count(fabric, channel["id"]) == 5


async def test_a_refused_batch_leaves_no_partial_state_and_the_corrected_retry_works(fabric: Fabric) -> None:
    """Partial failure inside a batch is reported, not hidden; nothing that was refused landed, and the
    retry that fixes the bad row is accepted without colliding with the good ones."""

    channel = await _channel(fabric, fabric.alpha)
    good = _batch(3)
    broken = [*good, {"ts": BASE_TS.isoformat(), "value": 1.0, "unit": "furlongs", "source_point_id": "bad"}]

    first = (await _append(fabric, fabric.alpha, channel["id"], broken)).json()
    assert first["received"] == 4
    assert first["accepted"] == 3
    assert first["rejected"] == 1
    assert first["rejections"][0]["index"] == 3
    assert await _row_count(fabric, channel["id"]) == 3

    corrected = await _append(
        fabric,
        fabric.alpha,
        channel["id"],
        [{"ts": BASE_TS.isoformat(), "value": 1.0, "unit": "psi", "source_point_id": "bad"}],
    )
    assert corrected.status_code == 201, corrected.text
    assert corrected.json()["accepted"] == 1
    assert await _row_count(fabric, channel["id"]) == 4

    replay = (await _append(fabric, fabric.alpha, channel["id"], broken)).json()
    assert replay["duplicates"] == 3, "the good rows were already stored and are recognised as such"
    assert replay["rejected"] == 1


async def test_parallel_appends_to_two_tenants_do_not_see_each_other(fabric: Fabric) -> None:
    """Two organizations ingesting at the same instant: same channel key, same source point ids, no
    leakage in either direction."""

    alpha = await _channel(fabric, fabric.alpha)
    bravo = await _channel(fabric, fabric.bravo)

    async def send(tenant, series_id, value: float):
        return await _append(fabric, tenant, series_id, _batch(10, value=value))

    first, second = await asyncio.gather(
        send(fabric.alpha, alpha["id"], 3500.0),
        send(fabric.bravo, bravo["id"], 6000.0),
    )
    assert first.status_code == second.status_code == 201, (first.text, second.text)
    assert first.json()["accepted"] == second.json()["accepted"] == 10
    assert await _row_count(fabric, alpha["id"]) == 10
    assert await _row_count(fabric, bravo["id"]) == 10

    latest = (
        await fabric.http.get(
            f"{PREFIX}/wells/{fabric.alpha.well_id}/timeseries/latest", headers=fabric.alpha.headers
        )
    ).json()
    assert latest["total"] == 1
    assert latest["items"][0]["channel_id"] == alpha["id"]
    assert latest["items"][0]["value"] == pytest.approx(3509.0 * PSI_IN_PA, rel=1e-9)


async def test_a_channel_created_while_a_batch_is_appended_is_the_same_channel(fabric: Fabric) -> None:
    """The create/append race the mission names: a caller registers the channel and a caller appends to
    it at the same time. The append either finds the channel or is told, cleanly, that it is not there —
    never a 500 and never a stray second channel."""

    create = fabric.http.post(
        f"{PREFIX}/timeseries",
        json={
            "well_id": fabric.alpha.well_id,
            "channel_key": "spp",
            "name": "Standpipe pressure",
            "dimension": "pressure",
            "unit": "psi",
        },
        headers=fabric.alpha.headers,
    )
    created = await create
    assert created.status_code == 201, created.text
    channel_id = created.json()["id"]

    append, again = await asyncio.gather(
        _append(fabric, fabric.alpha, channel_id, _batch(4)),
        fabric.http.post(
            f"{PREFIX}/timeseries",
            json={
                "well_id": fabric.alpha.well_id,
                "channel_key": "spp",
                "name": "Standpipe pressure",
                "dimension": "pressure",
                "unit": "psi",
            },
            headers=fabric.alpha.headers,
        ),
    )
    assert append.status_code == 201, append.text
    assert again.status_code == 201, again.text
    assert again.json()["id"] == channel_id
    assert again.json()["created"] is False
    assert await _channel_count(fabric, fabric.alpha, "spp") == 1
    assert await _row_count(fabric, channel_id) == 4


async def test_appending_to_a_channel_that_does_not_exist_is_a_clean_404(fabric: Fabric) -> None:
    response = await _append(fabric, fabric.alpha, "tms_does_not_exist", _batch(1))
    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "platform.not_found"


async def test_a_batch_equal_to_the_limit_is_accepted_and_one_over_is_refused(fabric: Fabric) -> None:
    """The boundary, both sides of it: 5 000 points land, 5 001 do not, and the refusal is a refusal
    rather than a silent truncation to the first 5 000."""

    channel = await _channel(fabric, fabric.alpha)
    points = _batch(5000)
    accepted = await _append(fabric, fabric.alpha, channel["id"], points)
    assert accepted.status_code == 201, accepted.text
    assert accepted.json()["accepted"] == 5000
    assert await _row_count(fabric, channel["id"]) == 5000

    over = await fabric.http.post(
        f"{PREFIX}/timeseries/{channel['id']}/points",
        json={"points": _batch(5001, prefix="q")},
        headers=fabric.alpha.headers,
    )
    assert over.status_code == 422, over.text
    assert await _row_count(fabric, channel["id"]) == 5000, "the refused batch must not have added a row"
