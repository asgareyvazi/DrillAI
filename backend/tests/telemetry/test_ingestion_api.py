"""Ingestion over HTTP: what was accepted, what was refused, and why.

Everything here goes through the real application with two organizations, because the claims being made
are about an API's behaviour under real conditions: a race, a replay, a conflicting value, a unit the
platform cannot convert and a point that arrives two days late. The counters in the response are checked
against rows in the database — a reconciliation report that does not reconcile is worse than none.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import func, select
from tests.fixtures.fabric import Fabric

from drillai.db.models import AuditLog, TimeSeries, TimeSeriesPoint

PREFIX = "/api/v1"
BASE_TS = dt.datetime(2026, 3, 15, 8, 0, tzinfo=dt.UTC)


def _points(count: int, *, start: dt.datetime = BASE_TS, step_seconds: int = 60, value: float = 3500.0):
    return [
        {
            "ts": (start + dt.timedelta(seconds=step_seconds * index)).isoformat(),
            "value": value + index,
            "unit": "psi",
        }
        for index in range(count)
    ]


async def _create_channel(
    fabric: Fabric,
    tenant,
    *,
    channel_key: str = "spp",
    dimension: str = "pressure",
    unit: str = "psi",
    wellbore_id: str | None = None,
    operation_id: str | None = None,
    name: str = "Standpipe pressure",
) -> dict:
    response = await fabric.http.post(
        f"{PREFIX}/timeseries",
        json={
            "well_id": tenant.well_id,
            "wellbore_id": wellbore_id,
            "operation_id": operation_id,
            "channel_key": channel_key,
            "name": name,
            "dimension": dimension,
            "unit": unit,
            "is_realtime": True,
            "source": "witsml",
        },
        headers=tenant.headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _append(fabric: Fabric, tenant, series_id: str, points: list[dict], **extra) -> dict:
    response = await fabric.http.post(
        f"{PREFIX}/timeseries/{series_id}/points",
        json={"points": points, **extra},
        headers=tenant.headers,
    )
    return response


async def _rows(fabric: Fabric, series_id: str) -> int:
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


async def test_a_channel_is_registered_with_its_canonical_unit_and_scope(fabric: Fabric) -> None:
    channel = await _create_channel(fabric, fabric.alpha)
    assert channel["id"].startswith("tms_")
    assert channel["unit"] == "Pa", "the canonical unit for pressure is stored, not the source's psi"
    assert channel["source_unit"] == "psi"
    assert channel["scope"] == fabric.alpha.well_id
    assert channel["created"] is True
    assert channel["channel_key"] == "spp"


async def test_creating_the_same_channel_twice_returns_the_same_one(fabric: Fabric) -> None:
    """A retried creation is not an error and not a second channel."""

    first = await _create_channel(fabric, fabric.alpha)
    second = await _create_channel(fabric, fabric.alpha)
    assert second["id"] == first["id"]
    assert second["created"] is False

    async with fabric.session() as session:
        rows = (
            (
                await session.execute(
                    select(TimeSeries).where(TimeSeries.org_id == fabric.org_id(fabric.alpha))
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 1


async def test_a_channel_for_another_tenants_well_is_not_found(fabric: Fabric) -> None:
    response = await fabric.http.post(
        f"{PREFIX}/timeseries",
        json={
            "well_id": fabric.bravo.well_id,
            "channel_key": "spp",
            "name": "Standpipe pressure",
            "dimension": "pressure",
            "unit": "psi",
        },
        headers=fabric.alpha.headers,
    )
    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "platform.not_found"


async def test_a_channel_whose_unit_the_dimension_cannot_convert_is_refused(fabric: Fabric) -> None:
    response = await fabric.http.post(
        f"{PREFIX}/timeseries",
        json={
            "well_id": fabric.alpha.well_id,
            "channel_key": "spp",
            "name": "Standpipe pressure",
            "dimension": "pressure",
            "unit": "furlongs",
        },
        headers=fabric.alpha.headers,
    )
    assert response.status_code == 422, response.text
    body = response.json()["error"]
    assert body["code"] == "platform.validation_failed"
    assert "furlongs" in body["message"]


async def test_a_batch_reconciles_every_number_it_reports(fabric: Fabric) -> None:
    channel = await _create_channel(fabric, fabric.alpha)
    response = await _append(fabric, fabric.alpha, channel["id"], _points(10))
    assert response.status_code == 201, response.text
    report = response.json()

    assert report["received"] == 10
    assert report["accepted"] == 10
    assert report["duplicates"] == 0
    assert report["rejected"] == 0
    assert report["reconciled"] is True
    assert report["quality_counts"] == {"good": 10}
    assert report["first_ts"] == BASE_TS.isoformat()
    assert report["last_ts"] == (BASE_TS + dt.timedelta(seconds=540)).isoformat()
    assert await _rows(fabric, channel["id"]) == 10, "the report must match the rows it describes"


async def test_values_are_stored_in_canonical_units_and_keep_the_source(fabric: Fabric) -> None:
    channel = await _create_channel(fabric, fabric.alpha)
    await _append(fabric, fabric.alpha, channel["id"], [{"ts": BASE_TS.isoformat(), "value": 3500.0, "unit": "psi"}])

    async with fabric.session() as session:
        point = (
            await session.execute(
                select(TimeSeriesPoint).where(TimeSeriesPoint.series_id == channel["id"])
            )
        ).scalar_one()
    assert point.value == pytest.approx(3500.0 * 6894.757293168, rel=1e-9)
    assert point.src_value == 3500.0
    assert point.src_unit == "psi"


async def test_a_replayed_point_is_a_duplicate_not_a_second_row(fabric: Fabric) -> None:
    channel = await _create_channel(fabric, fabric.alpha)
    points = [{"ts": BASE_TS.isoformat(), "value": 3500.0, "unit": "psi", "source_point_id": "p-1"}]

    first = (await _append(fabric, fabric.alpha, channel["id"], points)).json()
    second = (await _append(fabric, fabric.alpha, channel["id"], points)).json()

    assert first["accepted"] == 1
    assert second["accepted"] == 0
    assert second["duplicates"] == 1
    assert second["reconciled"] is True
    assert await _rows(fabric, channel["id"]) == 1


async def test_a_conflicting_replay_is_reported_and_neither_stored_nor_overwritten(fabric: Fabric) -> None:
    """The same source identity with a different value is a conflict, and the default policy refuses it.

    A silent overwrite would lose the reading the platform already acted on; a silent second row would
    make "the value at 08:00" ambiguous. The caller is told, and decides.
    """

    channel = await _create_channel(fabric, fabric.alpha)
    await _append(
        fabric,
        fabric.alpha,
        channel["id"],
        [{"ts": BASE_TS.isoformat(), "value": 3500.0, "unit": "psi", "source_point_id": "p-1"}],
    )
    conflict = await _append(
        fabric,
        fabric.alpha,
        channel["id"],
        [{"ts": BASE_TS.isoformat(), "value": 3625.0, "unit": "psi", "source_point_id": "p-1"}],
    )
    report = conflict.json()
    assert report["accepted"] == 0
    assert report["rejected"] == 1
    assert report["conflicts"], "a conflict is named in the report, not folded into 'rejected'"

    async with fabric.session() as session:
        point = (
            await session.execute(
                select(TimeSeriesPoint).where(TimeSeriesPoint.series_id == channel["id"])
            )
        ).scalar_one()
    assert point.value == pytest.approx(3500.0 * 6894.757293168, rel=1e-9), "the first value stands"


async def test_the_revise_policy_records_the_previous_value_in_the_row(fabric: Fabric) -> None:
    """The other honest answer to a conflict: the source is correcting itself, and the correction keeps
    the value it replaced."""

    channel = await _create_channel(fabric, fabric.alpha)
    await _append(
        fabric, fabric.alpha, channel["id"],
        [{"ts": BASE_TS.isoformat(), "value": 3500.0, "unit": "psi", "source_point_id": "p-1"}],
    )
    revised = await _append(
        fabric, fabric.alpha, channel["id"],
        [{"ts": BASE_TS.isoformat(), "value": 3625.0, "unit": "psi", "source_point_id": "p-1"}],
        on_conflict="revise",
    )
    report = revised.json()
    assert report["revised"] == 1
    assert report["accepted"] == 0

    async with fabric.session() as session:
        point = (
            await session.execute(
                select(TimeSeriesPoint).where(TimeSeriesPoint.series_id == channel["id"])
            )
        ).scalar_one()
    assert point.value == pytest.approx(3625.0 * 6894.757293168, rel=1e-9)
    revisions = (point.attributes or {})["revisions"]
    assert revisions[0]["value"] == pytest.approx(3500.0 * 6894.757293168, rel=1e-9)


async def test_a_late_point_is_accepted_and_marked_late(fabric: Fabric) -> None:
    channel = await _create_channel(fabric, fabric.alpha)
    taken = dt.datetime(2026, 3, 13, 9, 0, tzinfo=dt.UTC)
    arrived = taken + dt.timedelta(hours=36)
    report = (
        await _append(
            fabric,
            fabric.alpha,
            channel["id"],
            [{"ts": taken.isoformat(), "value": 3400.0, "unit": "psi", "received_at": arrived.isoformat()}],
        )
    ).json()
    assert report["accepted"] == 1
    assert report["late"] == 1

    async with fabric.session() as session:
        point = (
            await session.execute(
                select(TimeSeriesPoint).where(TimeSeriesPoint.series_id == channel["id"])
            )
        ).scalar_one()
    assert point.is_late is True
    assert point.ts == taken, "a late point is not moved in time to when it arrived"
    assert point.received_at == arrived


async def test_out_of_order_is_its_own_fact_and_does_not_move_the_horizon(fabric: Fabric) -> None:
    """Arriving late and arriving out of order are different things, and the platform says which.

    ``is_out_of_order`` is relative to the newest measurement the channel holds; ``is_late`` is relative
    to when that measurement was taken. A reading that arrived promptly but describes an older moment
    than one already stored is out of order and *not* late — and a point that is out of order is still
    stored: refusing it would tear a hole in a timeline the platform was asked to keep.
    """

    channel = await _create_channel(fabric, fabric.alpha)
    for minute in (0, 30, 60):
        moment = BASE_TS + dt.timedelta(minutes=minute)
        await _append(
            fabric,
            fabric.alpha,
            channel["id"],
            [{"ts": moment.isoformat(), "value": 3500.0 + minute, "unit": "psi",
              "received_at": (moment + dt.timedelta(seconds=5)).isoformat(),
              "source_point_id": f"p-{minute}"}],
        )

    older = BASE_TS + dt.timedelta(minutes=15)
    report = (
        await _append(
            fabric,
            fabric.alpha,
            channel["id"],
            [{"ts": older.isoformat(), "value": 9999.0, "unit": "psi",
              "received_at": (older + dt.timedelta(seconds=5)).isoformat(), "source_point_id": "p-15"}],
        )
    ).json()
    assert report["accepted"] == 1
    assert report["out_of_order"] == 1
    assert report["late"] == 0, "it arrived seconds after it was taken; it is old, not late"

    async with fabric.session() as session:
        point = (
            await session.execute(
                select(TimeSeriesPoint).where(TimeSeriesPoint.source_point_id == "p-15")
            )
        ).scalar_one()
        channel_row = (
            await session.execute(select(TimeSeries).where(TimeSeries.id == channel["id"]))
        ).scalar_one()
    assert point.is_out_of_order is True
    assert point.is_late is False
    assert channel_row.last_ts == BASE_TS + dt.timedelta(minutes=60), (
        "the channel's horizon is the newest measurement, not the newest arrival"
    )
    assert channel_row.first_ts == BASE_TS


async def test_a_batch_that_straddles_the_horizon_marks_only_the_older_points(fabric: Fabric) -> None:
    """Points inside one batch are compared against each other and against what is already stored, so a
    batch that replays an old window and then continues does not tag its newest point as out of order."""

    channel = await _create_channel(fabric, fabric.alpha)
    await _append(
        fabric, fabric.alpha, channel["id"],
        [{"ts": BASE_TS.isoformat(), "value": 3500.0, "unit": "psi", "source_point_id": "base"}],
    )
    moments = [BASE_TS + dt.timedelta(seconds=offset) for offset in (30, 60, 90)]
    report = (
        await _append(
            fabric,
            fabric.alpha,
            channel["id"],
            [
                {
                    "ts": moment.isoformat(),
                    "value": 3500.0 + index,
                    "unit": "psi",
                    "received_at": (moment + dt.timedelta(seconds=5)).isoformat(),
                    "source_point_id": f"p-{index}",
                }
                for index, moment in enumerate(moments)
            ],
        )
    ).json()
    assert report["accepted"] == 3
    assert report["out_of_order"] == 0, "each point is newer than every point already accepted"

    async with fabric.session() as session:
        flags = (
            await session.execute(
                select(TimeSeriesPoint.is_out_of_order).where(TimeSeriesPoint.source_point_id == "p-0")
            )
        ).scalar_one()
        channel_row = (
            await session.execute(select(TimeSeries).where(TimeSeries.id == channel["id"]))
        ).scalar_one()
    assert flags is False
    assert channel_row.last_ts == BASE_TS + dt.timedelta(seconds=90)


async def test_a_point_with_no_unit_uses_the_channels_declared_source_unit(fabric: Fabric) -> None:
    """A bare number is not refused, but it is also not assumed to be canonical: it is read in the unit
    the channel declared its source speaks, which is the only unit that makes it a measurement."""

    channel = await _create_channel(fabric, fabric.alpha)
    report = (
        await _append(fabric, fabric.alpha, channel["id"], [{"ts": BASE_TS.isoformat(), "value": 3500.0}])
    ).json()
    assert report["accepted"] == 1

    async with fabric.session() as session:
        point = (
            await session.execute(
                select(TimeSeriesPoint).where(TimeSeriesPoint.series_id == channel["id"])
            )
        ).scalar_one()
    assert point.src_value == 3500.0
    assert point.src_unit == "psi", "what the source sent is kept as the source sent it"
    assert point.value == pytest.approx(3500.0 * 6894.757293168, rel=1e-9)

    other = await _create_channel(
        fabric, fabric.alpha, channel_key="mud_weight", dimension="mud_weight", unit="ppg",
        name="Mud weight",
    )
    refused = (
        await _append(
            fabric, fabric.alpha, other["id"],
            [{"ts": BASE_TS.isoformat(), "value": 9.2, "unit": "smell"}],
        )
    ).json()
    assert refused["rejected"] == 1
    assert refused["rejections"][0]["field"] == "unit"


async def test_a_partly_invalid_batch_accepts_the_valid_points_and_reports_the_rest(fabric: Fabric) -> None:
    channel = await _create_channel(fabric, fabric.alpha)
    points = [
        {"ts": BASE_TS.isoformat(), "value": 3500.0, "unit": "psi"},
        {"ts": BASE_TS.isoformat(), "value": 3501.0, "unit": "smell"},
        {"ts": (BASE_TS + dt.timedelta(seconds=60)).isoformat(), "value": 3502.0, "unit": "psi"},
        {"ts": BASE_TS.isoformat(), "value": 3503.0, "unit": "psi", "quality": "brilliant"},
    ]
    report = (await _append(fabric, fabric.alpha, channel["id"], points)).json()
    assert report["received"] == 4
    assert report["accepted"] == 2
    assert report["rejected"] == 2
    assert report["reconciled"] is True
    assert {item["reason"] for item in report["rejections"]} and all(
        "index" in item for item in report["rejections"]
    )
    assert await _rows(fabric, channel["id"]) == 2


async def test_a_batch_larger_than_the_limit_is_refused_not_truncated(fabric: Fabric) -> None:
    channel = await _create_channel(fabric, fabric.alpha)
    too_many = _points(5)[0]
    response = await fabric.http.post(
        f"{PREFIX}/timeseries/{channel['id']}/points",
        json={"points": [too_many for _ in range(5001)]},
        headers=fabric.alpha.headers,
    )
    assert response.status_code == 422, response.text
    # FastAPI's own body validation refuses first, which is the same refusal one layer earlier; either
    # way the batch does not land partially.
    assert await _rows(fabric, channel["id"]) == 0


async def test_an_idempotency_key_makes_a_retried_batch_a_replay(fabric: Fabric) -> None:
    channel = await _create_channel(fabric, fabric.alpha)
    key = "ui-batch-0001"
    first = await fabric.http.post(
        f"{PREFIX}/timeseries/{channel['id']}/points",
        json={"points": _points(3)},
        headers={**fabric.alpha.headers, "Idempotency-Key": key},
    )
    second = await fabric.http.post(
        f"{PREFIX}/timeseries/{channel['id']}/points",
        json={"points": _points(3)},
        headers={**fabric.alpha.headers, "Idempotency-Key": key},
    )
    assert first.status_code == 201
    assert second.status_code == 201
    assert second.json() == first.json(), "the stored answer is replayed, not recomputed"
    assert await _rows(fabric, channel["id"]) == 3


async def test_the_batch_leaves_one_audit_row_with_the_counts_and_not_the_payload(fabric: Fabric) -> None:
    channel = await _create_channel(fabric, fabric.alpha)
    await _append(fabric, fabric.alpha, channel["id"], _points(4))

    async with fabric.session() as session:
        rows = (
            (
                await session.execute(
                    select(AuditLog).where(
                        AuditLog.resource_id == channel["id"], AuditLog.action == "timeseries.append"
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 1
    assert rows[0].after["accepted"] == 4
    assert "points" not in (rows[0].details or {}), "the ledger records the mutation, not the data"
