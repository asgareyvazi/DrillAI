"""Scale: what the telemetry path costs when a well has a hundred thousand measurements.

The number of points a test writes is configurable (``DRILLAI_TELEMETRY_SCALING_POINTS``) so that an
ordinary run stays quick while a deliberate run can go to half a million or a million:

    DRILLAI_TELEMETRY_SCALING_POINTS=500000 .venv/bin/python -m pytest tests/telemetry/test_scaling.py

Three things are proved, and none of them by looking at the clock alone:

* **The writes land and reconcile.** Every batch reports counts that match the rows it wrote, at scale.
* **The read cost does not grow with the series.** A page of a hundred points is the same number of
  statements whether the channel holds a thousand measurements or a hundred thousand, and the plan the
  database chose is inspected, so "it used the index" is evidence rather than hope.
* **Latest is one statement per channel *set*, not per channel.** Asserted against the statement counter
  at a scale where an accidental per-channel or per-point loop cannot hide.
"""

from __future__ import annotations

import datetime as dt
import os
import time

from sqlalchemy import text
from tests.fixtures.fabric import Fabric
from tests.telemetry.conftest import statement_counter

PREFIX = "/api/v1"
BASE_TS = dt.datetime(2026, 3, 15, 0, 0, tzinfo=dt.UTC)

#: 120 000 by default: comfortably past the mission's floor, and a few seconds on SQLite. The report's
#: larger runs are the same code path with this raised.
POINTS = int(os.environ.get("DRILLAI_TELEMETRY_SCALING_POINTS", "120000"))
BATCH = 5000


async def _channel(fabric: Fabric, tenant, *, channel_key: str = "spp") -> dict:
    response = await fabric.http.post(
        f"{PREFIX}/timeseries",
        json={
            "well_id": tenant.well_id,
            "channel_key": channel_key,
            "name": "Standpipe pressure",
            "dimension": "pressure",
            "unit": "psi",
            "source": "witsml",
        },
        headers=tenant.headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _ingest(fabric: Fabric, tenant, series_id: str, total: int) -> dict:
    """Write ``total`` measurements in batches, through the API, and reconcile the answers as we go."""

    written = 0
    statements = 0
    started = time.perf_counter()
    for offset in range(0, total, BATCH):
        size = min(BATCH, total - offset)
        points = [
            {
                "ts": (BASE_TS + dt.timedelta(seconds=offset + index)).isoformat(),
                "value": 3000.0 + ((offset + index) % 500),
                "unit": "psi",
                "source_point_id": f"p-{offset + index}",
            }
            for index in range(size)
        ]
        with statement_counter(fabric) as recorded:
            response = await fabric.http.post(
                f"{PREFIX}/timeseries/{series_id}/points",
                json={"points": points},
                headers=tenant.headers,
            )
        assert response.status_code == 201, response.text
        report = response.json()
        assert report["received"] == size
        assert report["accepted"] == size, report
        assert report["reconciled"] is True
        statements += len(recorded)
        written += report["accepted"]
    return {
        "written": written,
        "statements": statements,
        "seconds": time.perf_counter() - started,
    }


async def test_a_large_series_ingests_reconciled_and_reads_in_bounded_statements(
    fabric: Fabric, record_property
) -> None:
    channel = await _channel(fabric, fabric.alpha)
    ingest = await _ingest(fabric, fabric.alpha, channel["id"], POINTS)
    assert ingest["written"] == POINTS
    record_property("points_ingested", POINTS)
    record_property("ingest_seconds", round(ingest["seconds"], 2))
    record_property("ingest_statements", ingest["statements"])
    record_property(
        "points_per_second", int(POINTS / ingest["seconds"]) if ingest["seconds"] else None
    )
    print(
        f"[scaling] {POINTS} points in {ingest['seconds']:.1f}s "
        f"({POINTS / ingest['seconds']:.0f} points/s, {ingest['statements']} statements)"
    )

    # ---- the write cost, stated as a ratio rather than as a magic number: a batch of five thousand is
    # a bounded number of statements per *chunk of identities*, plus the channel read and the ledger row.
    batches = -(-POINTS // BATCH)
    per_batch = ingest["statements"] / batches
    assert per_batch <= 60, f"{per_batch:.1f} statements per batch of {BATCH}"

    # ---- the read cost: identical for a small page at the start and a page deep inside the series.
    async def page_statements(params: dict) -> list[str]:
        with statement_counter(fabric) as recorded:
            response = await fabric.http.get(
                f"{PREFIX}/timeseries/{channel['id']}/points",
                params=params,
                headers=fabric.alpha.headers,
            )
            assert response.status_code == 200, response.text
        assert response.json()["returned"] == 100
        return [
            statement
            for statement in recorded
            if "time_series_points" in statement and statement.lower().startswith("select")
        ]

    first_page = await page_statements({"limit": 100})
    record_property("page_statements", len(first_page))
    # A page in the middle: since moves the anchor deep into the series.
    middle = (BASE_TS + dt.timedelta(seconds=POINTS // 2)).isoformat()
    deep_page = await page_statements({"limit": 100, "since": middle})
    assert len(first_page) == len(deep_page) == 2, (
        "a page must be a count plus one page read; got "
        f"{len(first_page)} at the start and {len(deep_page)} in the middle"
    )

    # ---- and the database's own plan for that read: the (series_id, ts) index, not a scan of 120 000 rows.
    async with fabric.session() as session:
        plan = (
            await session.execute(
                text(
                    "EXPLAIN QUERY PLAN SELECT * FROM time_series_points "
                    "WHERE series_id = :series AND ts >= :since ORDER BY ts, id LIMIT 101"
                ),
                {"series": channel["id"], "since": BASE_TS},
            )
        ).all()
    details = " | ".join(str(row[-1]) for row in plan)
    assert "ix_time_series_points_series_ts" in details or "USING INDEX" in details.upper(), details
    assert "SCAN time_series_points" not in details.upper() or "USING INDEX" in details.upper(), details


async def test_latest_over_a_large_series_is_one_statement_for_the_whole_channel_set(
    fabric: Fabric, record_property
) -> None:
    """Forty channels, a hundred and twenty thousand measurements, and the newest-per-channel answer costs
    one query against the points table — the shape that makes a streaming well affordable."""

    channels = []
    for index in range(40):
        channel = await _channel(fabric, fabric.alpha, channel_key=f"ch_{index:02d}")
        channels.append(channel["id"])
    for channel_id in channels:
        points = [
            {
                "ts": (BASE_TS + dt.timedelta(seconds=1000 * index)).isoformat(),
                "value": 3000.0 + index,
                "unit": "psi",
                "source_point_id": f"{channel_id}-{index}",
            }
            for index in range(POINTS // len(channels))
        ]
        for offset in range(0, len(points), BATCH):
            response = await fabric.http.post(
                f"{PREFIX}/timeseries/{channel_id}/points",
                json={"points": points[offset : offset + BATCH]},
                headers=fabric.alpha.headers,
            )
            assert response.status_code == 201, response.text

    with statement_counter(fabric) as recorded:
        response = await fabric.http.get(
            f"{PREFIX}/wells/{fabric.alpha.well_id}/timeseries/latest",
            params={"limit": 40},
            headers=fabric.alpha.headers,
        )
        assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 40
    point_statements = [
        statement for statement in recorded if "time_series_points" in statement
    ]
    record_property("latest_point_statements", len(point_statements))
    print(f"[scaling] latest over 40 channels: {len(point_statements)} statement(s) on the points table")
    assert len(point_statements) == 1, (
        "the newest-per-channel read touched the points table more than once; "
        f"{len(point_statements)} statements for 40 channels"
    )
    # Every channel's newest reading is the last one written to it, and it is from March, so the answer
    # says so rather than calling it live.
    assert all(item["freshness"] == "stale" for item in body["items"])
    newest = {item["channel_id"]: item for item in body["items"]}
    assert newest[channels[0]]["observed_at"] == (
        BASE_TS + dt.timedelta(seconds=1000 * (POINTS // len(channels) - 1))
    ).isoformat()


async def test_reading_a_whole_series_in_pages_visits_every_point_exactly_once(fabric: Fabric) -> None:
    """A long series is read by paging, and paging at scale must not lose or repeat a measurement — the
    failure mode a cursor exists to prevent."""

    channel = await _channel(fabric, fabric.alpha)
    total = min(POINTS, 20000)
    await _ingest(fabric, fabric.alpha, channel["id"], total)

    seen = 0
    cursor: str | None = None
    pages = 0
    while True:
        params = {"limit": 1000}
        if cursor:
            params["cursor"] = cursor
        body = (
            await fabric.http.get(
                f"{PREFIX}/timeseries/{channel['id']}/points", params=params, headers=fabric.alpha.headers
            )
        ).json()
        seen += body["returned"]
        pages += 1
        cursor = body["next_cursor"]
        if cursor is None:
            break
    assert seen == total, f"{seen} points read across {pages} pages of a {total}-point series"
    assert pages == total // 1000
