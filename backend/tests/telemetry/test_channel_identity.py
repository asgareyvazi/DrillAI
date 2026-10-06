"""Channel identity, enforced by the database and by the service with the same rule.

The defect this replaces: ``UNIQUE (org_id, channel_key)`` allowed exactly one ``wob`` per tenant, so the
second well's weight on bit could not be recorded. The repair keeps one identity function and makes the
database enforce it, which is why these tests write rows and let the *database* refuse — a check that
lives only in the service is a check that a future bulk path will skip.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from tests.fixtures.fabric import Fabric

from drillai.core.errors import ValidationFailed
from drillai.core.ids import new_id
from drillai.db.models import TimeSeries
from drillai.telemetry.identity import channel_identity, scope_of, scope_token
from drillai.telemetry.units import CANONICAL_UNIT


def _channel(
    *,
    org_id: str,
    scope: dict[str, str | None],
    channel_key: str = "wob",
    dimension: str = "force",
    name: str = "Weight on bit",
) -> TimeSeries:
    return TimeSeries(
        id=new_id("tms"),
        org_id=org_id,
        well_id=scope["well_id"],
        wellbore_id=scope["wellbore_id"],
        operation_id=scope["operation_id"],
        scope_token=scope_token(**scope),  # type: ignore[arg-type]
        channel_key=channel_key,
        name=name,
        dimension=dimension,
        unit=CANONICAL_UNIT[dimension],
        source="manual",
        is_realtime=True,
    )


def test_the_same_key_in_two_wells_is_two_channels() -> None:
    """The whole point of the repair: two wells each have a weight on bit."""

    first = channel_identity(well_id="wel_a", channel_key="wob", dimension="force")
    second = channel_identity(well_id="wel_b", channel_key="wob", dimension="force")
    assert first != second
    assert first == "wel_a|wob|force"
    assert second == "wel_b|wob|force"


def test_a_well_channel_and_a_wellbore_channel_are_different_channels() -> None:
    """``depth_md`` for the well as a whole and ``depth_md`` for the 12¼" hole are two quantities with
    two sensors. Collapsing them would make the KPI strip show whichever was written last."""

    whole = channel_identity(well_id="wel_a", channel_key="depth_md", dimension="length")
    hole = channel_identity(
        well_id="wel_a", wellbore_id="wb_1", channel_key="depth_md", dimension="length"
    )
    assert whole != hole


def test_the_key_is_case_folded_but_the_dimension_is_not() -> None:
    """``WOB`` from a shouting acquisition system is the same channel; ``FORCE`` as a dimension is a
    typo the platform must not silently accept."""

    upper = channel_identity(well_id="wel_a", channel_key="WOB", dimension="force")
    lower = channel_identity(well_id="wel_a", channel_key="wob", dimension="force")
    assert upper == lower
    assert channel_identity(well_id="wel_a", channel_key="wob", dimension="FORCE") != lower


def test_the_same_key_in_two_dimensions_is_two_identities() -> None:
    """The dimension is what makes the conversion on read legal."""

    force = channel_identity(well_id="wel_a", channel_key="load", dimension="force")
    pressure = channel_identity(well_id="wel_a", channel_key="load", dimension="pressure")
    assert force != pressure


def test_a_channel_without_a_well_is_refused() -> None:
    with pytest.raises(ValidationFailed) as caught:
        scope_token(well_id=None)
    assert caught.value.details["field"] == "well_id"

    with pytest.raises(ValidationFailed):
        channel_identity(well_id=None, channel_key="wob", dimension="force")


def test_a_channel_without_a_key_is_refused() -> None:
    with pytest.raises(ValidationFailed) as caught:
        channel_identity(well_id="wel_a", channel_key="   ", dimension="force")
    assert caught.value.details["field"] == "channel_key"


def test_an_operation_scoped_channel_needs_its_wellbore() -> None:
    with pytest.raises(ValidationFailed) as caught:
        scope_token(well_id="wel_a", operation_id="opr_1")
    assert caught.value.details["field"] == "wellbore_id"


def test_the_token_round_trips_to_the_scope_it_names() -> None:
    for scope in (
        {"well_id": "wel_a", "wellbore_id": None, "operation_id": None},
        {"well_id": "wel_a", "wellbore_id": "wb_1", "operation_id": None},
        {"well_id": "wel_a", "wellbore_id": "wb_1", "operation_id": "opr_1"},
    ):
        assert scope_of(scope_token(**scope)) == scope  # type: ignore[arg-type]


async def test_the_database_refuses_a_duplicate_channel_identity(fabric: Fabric) -> None:
    """The constraint, not the service, is what makes this true under a race."""

    org = fabric.org_id(fabric.alpha)
    scope = {"well_id": fabric.alpha.well_id, "wellbore_id": None, "operation_id": None}
    async with fabric.session() as session:
        session.add(_channel(org_id=org, scope=scope))
        await session.commit()

    async with fabric.session() as session:
        session.add(_channel(org_id=org, scope=scope, name="Weight on bit (again)"))
        with pytest.raises(IntegrityError):
            await session.commit()


async def test_two_wells_may_each_have_their_own_channel_and_the_rows_are_distinct(fabric: Fabric) -> None:
    """The case the old constraint made impossible, now the normal case."""

    org = fabric.org_id(fabric.alpha)
    other_well = await fabric.add_well(fabric.alpha, "ALPHA-2")
    async with fabric.session() as session:
        session.add(
            _channel(
                org_id=org,
                scope={"well_id": fabric.alpha.well_id, "wellbore_id": None, "operation_id": None},
            )
        )
        session.add(
            _channel(
                org_id=org,
                scope={"well_id": other_well, "wellbore_id": None, "operation_id": None},
                name="Weight on bit (ALPHA-2)",
            )
        )
        await session.commit()

    async with fabric.session() as session:
        rows = (
            (await session.execute(select(TimeSeries).where(TimeSeries.org_id == org)))
            .scalars()
            .all()
        )
    assert sorted(row.well_id for row in rows) == sorted([fabric.alpha.well_id, other_well])
    assert len({row.scope_token for row in rows}) == 2


async def test_two_organizations_may_use_the_same_key_and_never_see_each_other(fabric: Fabric) -> None:
    alpha, bravo = fabric.org_id(fabric.alpha), fabric.org_id(fabric.bravo)
    async with fabric.session() as session:
        session.add(
            _channel(
                org_id=alpha,
                scope={"well_id": fabric.alpha.well_id, "wellbore_id": None, "operation_id": None},
            )
        )
        session.add(
            _channel(
                org_id=bravo,
                scope={"well_id": fabric.bravo.well_id, "wellbore_id": None, "operation_id": None},
            )
        )
        await session.commit()

    async with fabric.session() as session:
        alpha_rows = (
            (await session.execute(select(TimeSeries).where(TimeSeries.org_id == alpha)))
            .scalars()
            .all()
        )
    assert [row.well_id for row in alpha_rows] == [fabric.alpha.well_id]
    assert all(row.id != fabric.bravo.well_id for row in alpha_rows)


async def test_the_indexes_the_queries_depend_on_exist_on_both_engines(fabric: Fabric) -> None:
    """The access patterns are stated in the migration; this asserts the database has them, because an
    index that exists only in the model's metadata is an index nobody created."""

    from sqlalchemy import inspect

    def _names(connection) -> set[str]:
        inspector = inspect(connection)
        return {index["name"] for index in inspector.get_indexes("time_series")} | {
            index["name"] for index in inspector.get_indexes("time_series_points")
        }

    async with fabric.session() as session:
        names = await session.run_sync(lambda sync: _names(sync.connection()))
    assert {
        "ix_time_series_well_channel",
        "ix_time_series_bore_channel",
        "ix_time_series_points_series_ts",
        "ix_time_series_points_source_identity",
    } <= names
