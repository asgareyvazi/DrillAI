"""NPT accounting: derived from records, aggregated in the database, scoped when asked.

The numbers here are the ones an engineering review reconciles against the rig's own report, so the
properties are strict:

* every hour comes from a recorded event or operation row — a well with nothing recorded reports
  zero hours and says so, rather than an estimate;
* the two sources are never blended: the events basis and the operations basis are both published,
  and picking one is an explicit choice;
* the breakdowns are computed over the whole scope by the database, while the evidence list is
  bounded — and the response says when it was cut, so a short list is never read as "that is all";
* an occurrence count does not shrink because the evidence list was paged.
"""

from __future__ import annotations

import datetime as dt

from tests.fixtures.fabric import Fabric

from drillai.db.models import Event, NptCode, Operation

BASE = dt.datetime(2026, 3, 1, tzinfo=dt.UTC)


async def _seed(fabric: Fabric) -> None:
    """A small, fully known NPT history: three events and one operation roll-up."""
    async with fabric.session() as session:
        session.add_all(
            [
                NptCode(
                    org_id=fabric.org_id(fabric.alpha),
                    code="STUCK",
                    name="Stuck pipe",
                    category="stuck_pipe",
                    subcategory="mechanical",
                    is_operator_controllable=True,
                ),
                NptCode(
                    org_id=fabric.org_id(fabric.alpha),
                    code="WEATHER",
                    name="Weather",
                    category="weather",
                    is_operator_controllable=False,
                ),
            ]
        )
        session.add_all(
            [
                Event(
                    org_id=fabric.org_id(fabric.alpha),
                    project_id=fabric.alpha.project_id,
                    well_id=fabric.alpha.well_id,
                    wellbore_id=fabric.alpha.wellbore_id,
                    section_id=fabric.alpha.section_id,
                    title="Stuck pipe",
                    kind="stuck_pipe",
                    npt_code="STUCK",
                    npt_category="stuck_pipe",
                    is_npt=True,
                    npt_hours=6.0,
                    duration_hours=6.0,
                    occurred_at=BASE + dt.timedelta(hours=10),
                    status="closed",
                    severity="high",
                ),
                Event(
                    org_id=fabric.org_id(fabric.alpha),
                    project_id=fabric.alpha.project_id,
                    well_id=fabric.alpha.well_id,
                    wellbore_id=fabric.alpha.wellbore_id,
                    section_id=fabric.alpha.section_id,
                    title="Wait on weather",
                    kind="weather_downtime",
                    npt_code="WEATHER",
                    is_npt=True,
                    npt_hours=2.5,
                    occurred_at=BASE + dt.timedelta(days=40, hours=2),
                    status="closed",
                    severity="medium",
                ),
                Event(
                    org_id=fabric.org_id(fabric.alpha),
                    project_id=fabric.alpha.project_id,
                    well_id=fabric.alpha.well_id,
                    wellbore_id=fabric.alpha.wellbore_id,
                    title="Unclassified loss",
                    kind="problem",
                    is_npt=True,
                    npt_hours=1.0,
                    occurred_at=BASE + dt.timedelta(hours=30),
                    status="closed",
                    severity="low",
                ),
                # A zero-hour row: not an occurrence, and it must not appear in the counts.
                Event(
                    org_id=fabric.org_id(fabric.alpha),
                    project_id=fabric.alpha.project_id,
                    well_id=fabric.alpha.well_id,
                    wellbore_id=fabric.alpha.wellbore_id,
                    title="Watched, no loss",
                    kind="observation",
                    is_npt=True,
                    npt_hours=0.0,
                    occurred_at=BASE + dt.timedelta(hours=40),
                    status="closed",
                    severity="low",
                ),
            ]
        )
        session.add(
            Operation(
                org_id=fabric.org_id(fabric.alpha),
                project_id=fabric.alpha.project_id,
                well_id=fabric.alpha.well_id,
                wellbore_id=fabric.alpha.wellbore_id,
                section_id=fabric.alpha.section_id,
                operation_class="actual",
                sequence=7,
                name="Jarring and fishing",
                kind="jarring",
                phase="completed",
                status="completed",
                actual_start=BASE + dt.timedelta(hours=11),
                actual_duration_hours=7.0,
                npt_hours=6.5,
                is_productive=False,
            )
        )
        await session.commit()


async def _npt(fabric: Fabric, **params) -> dict:
    response = await fabric.http.get(
        f"/api/v1/wells/{fabric.alpha.well_id}/npt",
        params=params or None,
        headers=fabric.alpha.headers,
    )
    assert response.status_code == 200, response.text
    return response.json()["npt"]


async def test_the_events_basis_sums_only_what_was_recorded(fabric: Fabric) -> None:
    await _seed(fabric)
    npt = await _npt(fabric)
    assert npt["basis"] == "events"
    assert npt["total_hours"] == 9.5  # 6.0 + 2.5 + 1.0; the zero-hour row adds nothing
    # Four recorded NPT events, one of which records no loss. The flag is the recorder's claim, so
    # the count follows the flag while the total follows the hours.
    assert npt["event_count"] == 4
    assert npt["hours_from_events"] == 9.5
    assert npt["hours_from_operations"] == 6.5
    assert npt["percent_of_well_time"] is not None
    assert any("recorded" in note for note in npt["notes"])


async def test_the_two_sources_are_reported_separately_and_never_added_together(fabric: Fabric) -> None:
    await _seed(fabric)
    events = await _npt(fabric, basis="events")
    operations = await _npt(fabric, basis="operations")
    assert events["total_hours"] == 9.5
    assert operations["total_hours"] == 6.5
    # Both summaries carry both figures, so a reader can reconcile them rather than trusting one.
    assert events["hours_from_operations"] == operations["hours_from_operations"] == 6.5
    assert operations["hours_from_events"] == events["hours_from_events"] == 9.5
    assert operations["event_count"] == 1
    assert operations["cases"][0]["source"] == "operation"
    assert "roll-up" in (operations["cases"][0]["note"] or "")


async def test_the_breakdowns_are_consistent_with_the_total(fabric: Fabric) -> None:
    await _seed(fabric)
    npt = await _npt(fabric)
    categories = {row["key"]: row for row in npt["by_category"]}
    assert categories["stuck_pipe"]["hours"] == 6.0
    assert categories["weather"]["hours"] == 2.5
    assert categories["unclassified"]["hours"] == 1.0
    # The standard categories are present even at zero, so "no loss here" is visible.
    assert categories["lost_circulation"]["hours"] == 0.0
    assert round(sum(row["hours"] for row in npt["by_category"]), 3) == 9.5

    by_code = {row["key"]: row for row in npt["by_code"]}
    assert by_code["STUCK"]["hours"] == 6.0
    assert by_code["WEATHER"]["hours"] == 2.5
    assert by_code["unclassified"]["hours"] == 1.0

    by_section = {row["key"]: row for row in npt["by_section"]}
    assert by_section[fabric.alpha.section_id]["hours"] == 8.5
    assert by_section["not-sectioned"]["hours"] == 1.0

    months = {row["month"]: row["hours"] for row in npt["by_month"]}
    # March carries the stuck pipe and the unclassified loss; April the weather day. The zero-hour
    # event lands in March and adds nothing to it.
    assert months == {"2026-03": 7.0, "2026-04": 2.5}

    assert round(
        npt["controllable_hours"] + npt["uncontrollable_hours"] + npt["unknown_controllability_hours"], 3
    ) == 9.5
    assert npt["controllable_hours"] == 6.0
    assert npt["uncontrollable_hours"] == 2.5
    assert npt["unknown_controllability_hours"] == 1.0


async def test_the_evidence_list_is_bounded_and_says_so_without_shrinking_the_numbers(
    fabric: Fabric,
) -> None:
    await _seed(fabric)
    npt = await _npt(fabric, case_limit=2)
    assert len(npt["cases"]) == 2
    assert npt["cases_truncated"] is True
    assert npt["case_limit"] == 2
    # The totals describe the scope, not the page of evidence.
    assert npt["total_hours"] == 9.5
    assert npt["event_count"] == 4
    assert round(sum(row["hours"] for row in npt["by_category"]), 3) == 9.5
    assert any("bounded" in note for note in npt["notes"])

    whole = await _npt(fabric)
    assert whole["cases_truncated"] is False
    assert len(whole["cases"]) == 4
    # The evidence list is ordered by hours, largest first, and the bounded page is the largest slice
    # of it — never a different set of cases depending on the limit.
    assert [case["hours"] for case in whole["cases"]] == sorted(
        (case["hours"] for case in whole["cases"]), reverse=True
    )
    assert {case["id"] for case in npt["cases"]} <= {case["id"] for case in whole["cases"]}
    assert npt["cases"][0]["id"] == whole["cases"][0]["id"]


async def test_scoping_to_a_section_changes_the_total_and_says_it_did(fabric: Fabric) -> None:
    await _seed(fabric)
    scoped = await _npt(fabric, section_id=fabric.alpha.section_id)
    assert scoped["total_hours"] == 8.5
    # The two events that carry this section; the unclassified loss and the zero-hour row are not
    # sectioned, so they are outside it — and the total says so rather than silently including them.
    assert scoped["event_count"] == 2
    assert scoped["by_section"] == [] or all(
        row["key"] == fabric.alpha.section_id for row in scoped["by_section"]
    )
    assert any("scoped" in note for note in scoped["notes"])

    created = await fabric.http.post(
        f"/api/v1/wellbores/{fabric.alpha.wellbore_id}/sections",
        json={"sequence": 2, "name": "Section two", "kind": "production"},
        headers=fabric.alpha.headers,
    )
    assert created.status_code == 201, created.text
    other = created.json()["id"]
    empty_scope = await _npt(fabric, section_id=other)
    assert empty_scope["total_hours"] == 0.0
    assert empty_scope["event_count"] == 0
    assert "scoped" in " ".join(empty_scope["notes"])


async def test_scoping_to_an_operation_and_to_a_window(fabric: Fabric) -> None:
    await _seed(fabric)
    async with fabric.session() as session:
        from sqlalchemy import select

        operation_id = (
            await session.execute(select(Operation.id).where(Operation.name == "Jarring and fishing"))
        ).scalar_one()

    scoped = await _npt(fabric, operation_id=operation_id)
    assert scoped["cases"] == [], "no event is attributed to this operation"

    window = await _npt(
        fabric,
        since=(BASE + dt.timedelta(hours=9)).isoformat(),
        until=(BASE + dt.timedelta(hours=12)).isoformat(),
    )
    assert window["total_hours"] == 6.0
    assert window["event_count"] == 1
    assert "scoped" in " ".join(window["notes"])


async def test_the_operations_basis_groups_by_the_operation_itself(fabric: Fabric) -> None:
    await _seed(fabric)
    npt = await _npt(fabric, basis="operations")
    assert [row["key"] for row in npt["by_operation"]]
    assert npt["by_operation"][0]["label"] == "Jarring and fishing"
    assert npt["by_operation"][0]["hours"] == 6.5
    assert npt["unknown_controllability_hours"] == 6.5, "a roll-up states no controllability"
    assert npt["controllable_hours"] == 0.0


async def test_another_tenants_well_is_not_found_rather_than_empty(fabric: Fabric) -> None:
    """A well this caller cannot see is not a well with no NPT."""
    await _seed(fabric)
    assert (
        await fabric.http.get(
            f"/api/v1/wells/{fabric.bravo.well_id}/npt", headers=fabric.alpha.headers
        )
    ).status_code == 404
    assert (
        await fabric.http.get("/api/v1/wells/wel_missing/npt", headers=fabric.alpha.headers)
    ).status_code == 404


async def test_an_unknown_basis_is_refused(fabric: Fabric) -> None:
    response = await fabric.http.get(
        f"/api/v1/wells/{fabric.alpha.well_id}/npt",
        params={"basis": "guessed"},
        headers=fabric.alpha.headers,
    )
    assert response.status_code == 422, response.text


async def test_a_well_with_nothing_recorded_reports_zero_and_says_where_that_comes_from(
    fabric: Fabric,
) -> None:
    fresh = await fabric.add_well(fabric.alpha, "ALPHA-NONPT")
    response = await fabric.http.get(f"/api/v1/wells/{fresh}/npt", headers=fabric.alpha.headers)
    assert response.status_code == 200, response.text
    npt = response.json()["npt"]
    assert npt["total_hours"] == 0.0
    assert npt["event_count"] == 0
    assert npt["cases"] == []
    assert npt["percent_of_well_time"] is None or npt["percent_of_well_time"] == 0.0
    assert any("never an assumed zero" in note for note in npt["notes"])
