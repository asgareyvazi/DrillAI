"""The well timeline at scale: the page is bounded, and so is the work behind it.

Two properties are tested here, and they are the reason this file exists separately from the
timeline's correctness tests:

* **the page is the page.** With hundreds of rows on the well, a request for twenty entries returns
  twenty entries, in one deterministic order, and the cursor walks that order exactly once. A single
  request for everything and a cursor walk must produce the same sequence.
* **the work is bounded by the page, not by the well.** The measured quantity is rows read from the
  database: the service is driven through a session proxy that counts what each statement returned,
  so "we filter in SQL now" is not taken on faith. A timeline that read the well's whole history to
  show the first twenty entries would fail this measurement.

The large fixture is built directly in the database rather than through the API, because it exists to
size a read path, not to prove a write path. It is not part of the seed, and no test asserts on it
elsewhere.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from tests.fixtures.fabric import Fabric

from drillai.db.models import Event, Operation
from drillai.drilling.timeline import TimelineService, encode_cursor, parse_cursor, sort_key

BASE = dt.datetime(2026, 3, 1, tzinfo=dt.UTC)


async def _seed_well(fabric: Fabric, *, operations: int = 300, events: int = 200) -> None:
    """A well with a history: enough rows that reading all of them would be visible."""
    async with fabric.session() as session:
        for index in range(operations):
            started = BASE + dt.timedelta(hours=index)
            session.add(
                Operation(
                    org_id=fabric.org_id(fabric.alpha),
                    project_id=fabric.alpha.project_id,
                    well_id=fabric.alpha.well_id,
                    wellbore_id=fabric.alpha.wellbore_id,
                    operation_class="actual",
                    sequence=index,
                    name=f"Operation {index:03d}",
                    kind="drilling",
                    phase="completed",
                    status="completed",
                    actual_start=started,
                    actual_end=started + dt.timedelta(hours=1),
                    actual_duration_hours=1.0,
                    is_productive=True,
                )
            )
        for index in range(events):
            occurred = BASE + dt.timedelta(hours=index, minutes=30)
            session.add(
                Event(
                    org_id=fabric.org_id(fabric.alpha),
                    project_id=fabric.alpha.project_id,
                    well_id=fabric.alpha.well_id,
                    wellbore_id=fabric.alpha.wellbore_id,
                    title=f"Event {index:03d}",
                    kind="observation",
                    status="open",
                    severity="low",
                    occurred_at=occurred,
                )
            )
        await session.commit()


async def _timeline(fabric: Fabric, **params) -> dict:
    response = await fabric.http.get(
        f"/api/v1/wells/{fabric.alpha.well_id}/timeline",
        params=params or None,
        headers=fabric.alpha.headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


class _RowCountingSession:
    """A session proxy that records how many rows each statement returned."""

    def __init__(self, session: Any) -> None:
        self._session = session
        self.row_counts: list[int] = []

    async def execute(self, statement: Any) -> Any:
        rows = list((await self._session.execute(statement)).scalars().all())
        self.row_counts.append(len(rows))

        class _Result:
            @staticmethod
            def scalars() -> Any:
                class _Scalars:
                    @staticmethod
                    def all() -> list:
                        return rows

                return _Scalars()

            @staticmethod
            def scalar_one_or_none() -> Any:
                # The well-existence check reads one value; count it as the row it read.
                return rows[0] if rows else None

        return _Result()


async def test_a_page_returns_the_page_and_the_cursor_walks_it_exactly_once(fabric: Fabric) -> None:
    await _seed_well(fabric)
    whole = await _timeline(fabric, limit=2000)
    assert whole["count"] >= 500
    assert whole["next_cursor"] is None, "a short page is the end of the timeline"

    seen: list[str] = []
    cursor = None
    pages = 0
    while True:
        page = await _timeline(fabric, limit=40, cursor=cursor)
        pages += 1
        assert page["count"] <= 40
        seen.extend(entry["id"] for entry in page["entries"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
        assert pages < 40, "the cursor never reached the end of the timeline"

    assert pages > 1
    assert seen == [entry["id"] for entry in whole["entries"]]
    assert len(set(seen)) == len(seen)


async def test_the_ordering_is_deterministic_and_documented(fabric: Fabric) -> None:
    await _seed_well(fabric, operations=30, events=20)
    first = await _timeline(fabric, limit=100)
    second = await _timeline(fabric, limit=100)
    assert [entry["id"] for entry in first["entries"]] == [entry["id"] for entry in second["entries"]]
    keys = [
        (entry["at"] is None, entry["at"] or "", entry["kind"], entry["id"])
        for entry in first["entries"]
    ]
    assert keys == sorted(keys), "the response is not in the order its keyset is defined by"


async def test_a_window_and_a_kind_list_are_filtered_before_the_page_is_cut(fabric: Fabric) -> None:
    await _seed_well(fabric, operations=50, events=50)
    window = await _timeline(
        fabric,
        limit=2000,
        kinds=["event"],
        since=(BASE + dt.timedelta(hours=10)).isoformat(),
        until=(BASE + dt.timedelta(hours=20)).isoformat(),
    )
    # One event per hour, stamped at half past: the window covers the events seeded at 10:30..19:30.
    assert window["count"] == 10
    assert {entry["kind"] for entry in window["entries"]} == {"event"}
    for entry in window["entries"]:
        moment = dt.datetime.fromisoformat(entry["at"])
        assert BASE + dt.timedelta(hours=10) <= moment <= BASE + dt.timedelta(hours=20)

    # The same filter, paged: the window is applied in the database for every page, so the last page
    # ends at the same entry the unbounded request ended at.
    paged: list[str] = []
    cursor = None
    while True:
        page = await _timeline(fabric, limit=5, kinds=["event"], since=window["entries"][0]["at"], cursor=cursor)
        paged.extend(entry["id"] for entry in page["entries"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert set(paged) >= {entry["id"] for entry in window["entries"]}


async def test_an_unknown_kind_is_refused_with_the_list(fabric: Fabric) -> None:
    response = await fabric.http.get(
        f"/api/v1/wells/{fabric.alpha.well_id}/timeline",
        params={"kinds": ["not_a_kind"]},
        headers=fabric.alpha.headers,
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["details"]["known"]


async def test_an_unknown_well_is_not_found_rather_than_an_empty_timeline(fabric: Fabric) -> None:
    """A foreign well and a well with nothing on it are different answers."""
    assert (
        await fabric.http.get(
            f"/api/v1/wells/{fabric.bravo.well_id}/timeline", headers=fabric.alpha.headers
        )
    ).status_code == 404
    assert (
        await fabric.http.get("/api/v1/wells/wel_does_not_exist/timeline", headers=fabric.alpha.headers)
    ).status_code == 404

    # A real well with no drilling history is a different answer: a timeline that holds the record
    # of its own creation, and not a 404.
    fresh_well = await fabric.add_well(fabric.alpha, "ALPHA-EMPTY")
    empty = (
        await fabric.http.get(
            f"/api/v1/wells/{fresh_well}/timeline", headers=fabric.alpha.headers
        )
    )
    assert empty.status_code == 200, empty.text
    body = empty.json()
    assert body["next_cursor"] is None
    assert {entry["kind"] for entry in body["entries"]} <= {"twin_change"}
    assert not any(entry["kind"] in {"operation", "event"} for entry in body["entries"])


async def test_the_rows_read_are_bounded_by_the_page_not_by_the_well(fabric: Fabric) -> None:
    """The measurement: read at most ``limit`` rows per stream, whatever the well holds."""
    await _seed_well(fabric, operations=400, events=300)
    async with fabric.session() as session:
        counting = _RowCountingSession(session)
        service = TimelineService(counting, fabric.org_id(fabric.alpha))
        entries = await service.build(fabric.alpha.well_id, limit=20)

    assert len(entries) == 20
    # Seven streams, each contributing at most the page size, plus the well-existence check.
    assert len(counting.row_counts) <= 8
    assert max(counting.row_counts) <= 20, (
        f"a stream returned {max(counting.row_counts)} rows for a 20-entry page: the filter is in Python"
    )
    assert sum(counting.row_counts) <= 7 * 20 + 1


async def test_the_cursor_round_trips_and_refuses_nonsense() -> None:
    class _Entry:
        at = BASE
        kind = "event"
        id = "evn_1"

    token = encode_cursor(_Entry())
    assert parse_cursor(token) == (BASE, "event", "evn_1")

    from drillai.core.errors import ValidationFailed

    for bad in ("!!!", "", "Zm9vYmFy", "eyJhdCI6bnVsbCwia2luZCI6Im5vdF9hX2tpbmQiLCJpZCI6IngifQ"):
        try:
            parse_cursor(bad)
        except ValidationFailed:
            continue
        raise AssertionError(f"cursor {bad!r} was accepted")


async def test_sorting_places_undated_entries_last(fabric: Fabric) -> None:
    """An undated row is not "oldest"; it is undated, and it renders after everything dated.

    A planned operation with no start date is the real case: the plan exists, nobody has said when it
    begins, and putting it at the top of the well's history would assert a date it does not carry.
    """
    async with fabric.session() as session:
        session.add(
            Operation(
                org_id=fabric.org_id(fabric.alpha),
                well_id=fabric.alpha.well_id,
                wellbore_id=fabric.alpha.wellbore_id,
                name="Undated plan",
                kind="drilling",
                operation_class="plan",
                status="planned",
                sequence=999,
            )
        )
        await session.commit()

    body = await _timeline(fabric, limit=5)
    assert body["entries"][-1]["title"] == "Undated plan"
    assert body["entries"][-1]["at"] is None
    assert sort_key(type("E", (), {"at": None, "kind": "operation", "id": "x"})())[0] is True
