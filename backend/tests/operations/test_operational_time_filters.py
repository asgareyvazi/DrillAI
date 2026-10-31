"""Operation and event time filters use inclusive interval overlap, not independent endpoint tests."""

from __future__ import annotations

import datetime as dt

from tests.fixtures.fabric import Fabric

BASE = dt.datetime(2026, 4, 1, tzinfo=dt.UTC)


def _at(hour: int) -> str:
    return (BASE + dt.timedelta(hours=hour)).isoformat()


async def test_operation_window_uses_one_coherent_actual_or_planned_interval(fabric: Fabric) -> None:
    cases = [
        {
            "name": "actual crosses the whole window",
            "actual_start": _at(8),
            "actual_end": _at(14),
            "planned_start": _at(20),
            "planned_end": _at(22),
        },
        {
            # The independently-OR'ed old predicates combined this old actual end with a future
            # planned start and incorrectly admitted the row.
            "name": "actual ended before the window",
            "actual_start": _at(6),
            "actual_end": _at(9),
            "planned_start": _at(13),
            "planned_end": _at(15),
        },
        {
            # The reverse cross-pairing also used to admit this future actual interval.
            "name": "actual starts after the window",
            "actual_start": _at(13),
            "actual_end": _at(15),
            "planned_start": _at(6),
            "planned_end": _at(9),
        },
        {"name": "single actual timestamp inside the window", "actual_start": _at(11)},
        {"name": "undated actual operation"},
    ]
    created_ids: dict[str, str] = {}
    for case in cases:
        response = await fabric.http.post(
            "/api/v1/operations",
            json={
                "project_id": fabric.alpha.project_id,
                "well_id": fabric.alpha.well_id,
                "wellbore_id": fabric.alpha.wellbore_id,
                "operation_class": "actual",
                "kind": "drilling",
                **case,
            },
            headers=fabric.alpha.headers,
        )
        assert response.status_code == 201, response.text
        created_ids[case["name"]] = response.json()["id"]

    planned = await fabric.http.post(
        "/api/v1/operations",
        json={
            "project_id": fabric.alpha.project_id,
            "well_id": fabric.alpha.well_id,
            "wellbore_id": fabric.alpha.wellbore_id,
            "operation_class": "plan",
            "kind": "drilling",
            "name": "planned operation inside the window",
            "planned_start": _at(10),
            "planned_end": _at(12),
        },
        headers=fabric.alpha.headers,
    )
    assert planned.status_code == 201, planned.text
    created_ids["planned operation inside the window"] = planned.json()["id"]

    response = await fabric.http.get(
        "/api/v1/operations",
        params={"well_id": fabric.alpha.well_id, "since": _at(10), "until": _at(12)},
        headers=fabric.alpha.headers,
    )
    assert response.status_code == 200, response.text
    returned_ids = {item["id"] for item in response.json()["items"]}
    assert returned_ids == {
        created_ids["actual crosses the whole window"],
        created_ids["single actual timestamp inside the window"],
        created_ids["planned operation inside the window"],
    }
    assert response.json()["total"] == 3


async def test_event_window_includes_overlaps_and_excludes_outside_points(fabric: Fabric) -> None:
    cases = [
        ("event spans the window", 8, 14),
        ("event ended before the window", 6, 9),
        ("event starts after the window", 13, 15),
        ("point occurrence inside the window", 11, None),
        ("point occurrence after the window", 13, None),
    ]
    created_ids: dict[str, str] = {}
    for title, start, end in cases:
        response = await fabric.http.post(
            "/api/v1/events",
            json={
                "project_id": fabric.alpha.project_id,
                "well_id": fabric.alpha.well_id,
                "wellbore_id": fabric.alpha.wellbore_id,
                "title": title,
                "kind": "observation",
                "occurred_at": _at(start),
                "ended_at": _at(end) if end is not None else None,
            },
            headers=fabric.alpha.headers,
        )
        assert response.status_code == 201, response.text
        created_ids[title] = response.json()["id"]

    response = await fabric.http.get(
        "/api/v1/events",
        params={"well_id": fabric.alpha.well_id, "since": _at(10), "until": _at(12)},
        headers=fabric.alpha.headers,
    )
    assert response.status_code == 200, response.text
    returned_ids = {item["id"] for item in response.json()["items"]}
    assert returned_ids == {
        created_ids["event spans the window"],
        created_ids["point occurrence inside the window"],
    }
    assert response.json()["total"] == 2
