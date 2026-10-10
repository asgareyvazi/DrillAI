"""Point identity: what makes two recorded measurements the same one.

Three cases have to be distinguishable, and the platform previously could not tell any of them apart:

1. **a replay** — the same measurement sent twice — must collapse to one row;
2. **a correction** — a different value at the same instant — must not silently overwrite the first;
3. **two real readings** — same instant, different source — must both survive.

The identity rule is what decides, and it is enforced by a unique constraint so that two concurrent
writers cannot both win.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from tests.fixtures.fabric import Fabric

from drillai.core.ids import new_id
from drillai.db.models import TimeSeries, TimeSeriesPoint
from drillai.telemetry.identity import fingerprint, point_identity
from drillai.telemetry.units import CANONICAL_UNIT
from drillai.telemetry.vocabulary import ARRIVAL_QUALITY_STATES, QUALITY_STATES, TRUSTWORTHY_QUALITY

INSTANT = dt.datetime(2026, 3, 15, 9, 30, tzinfo=dt.UTC)


def test_a_point_from_a_source_with_ids_uses_that_id() -> None:
    identity = point_identity(
        series_id="tms_1", source_point_id="point-77", ts=INSTANT, value=1.0, source_ref="witsml://rig"
    )
    assert identity.identified_by == "source_point_id"
    assert identity.dedup_key == "point-77"


def test_a_point_from_a_source_without_ids_uses_a_documented_fingerprint() -> None:
    identity = point_identity(
        series_id="tms_1", source_point_id=None, ts=INSTANT, value=1.0, source_ref="csv"
    )
    assert identity.identified_by == "fingerprint"
    assert identity.fingerprint == fingerprint(
        series_id="tms_1", ts=INSTANT, value=1.0, source_ref="csv"
    )


def test_the_fingerprint_is_stable_across_equivalent_inputs() -> None:
    """The same measurement always hashes to the same string, including when the instant arrives in a
    different but equivalent form — which is what makes a re-sent frame collapse."""

    paris = dt.timezone(dt.timedelta(hours=1))
    same_instant_in_paris = INSTANT.astimezone(paris)
    assert fingerprint(series_id="tms_1", ts=INSTANT, value=1.5, source_ref="csv") == fingerprint(
        series_id="tms_1", ts=same_instant_in_paris, value=1.5, source_ref="csv"
    )


def test_a_different_value_at_the_same_instant_is_a_different_point() -> None:
    """Not a replay: the source is telling us something else about that moment. Collapsing it would
    discard a reading, so it is a distinct row and the service reports the conflict."""

    first = fingerprint(series_id="tms_1", ts=INSTANT, value=3500.0, source_ref="csv")
    second = fingerprint(series_id="tms_1", ts=INSTANT, value=3625.0, source_ref="csv")
    assert first != second


def test_two_sources_at_the_same_instant_are_two_points() -> None:
    """A rig sensor and a hand-entered reading are different measurements even at the same second, and
    the source reference is part of the identity for exactly that reason."""

    sensor = fingerprint(series_id="tms_1", ts=INSTANT, value=3500.0, source_ref="witsml://rig/1")
    hand = fingerprint(series_id="tms_1", ts=INSTANT, value=3500.0, source_ref="ddr:doc_1")
    assert sensor != hand


def test_a_source_sequence_separates_points_that_share_an_instant_and_value() -> None:
    """A source that samples twice inside one second and sends the same value twice: its own sequence
    is the only thing that tells the two readings apart."""

    first = fingerprint(series_id="tms_1", ts=INSTANT, value=3500.0, source_ref="csv", sequence=1)
    second = fingerprint(series_id="tms_1", ts=INSTANT, value=3500.0, source_ref="csv", sequence=2)
    assert first != second


def test_an_absent_value_is_part_of_the_identity() -> None:
    """A quality-only record (the sensor reported "no reading") is not the same point as a real zero."""

    missing = fingerprint(series_id="tms_1", ts=INSTANT, value=None, source_ref="csv")
    zero = fingerprint(series_id="tms_1", ts=INSTANT, value=0.0, source_ref="csv")
    assert missing != zero


def test_the_quality_vocabulary_is_internally_coherent() -> None:
    """One source of truth for quality, and the arrival states are a subset of it — a state the service
    can write must be a state the model declares."""

    assert set(ARRIVAL_QUALITY_STATES) <= set(QUALITY_STATES)
    assert set(TRUSTWORTHY_QUALITY) <= set(QUALITY_STATES)
    assert len(set(QUALITY_STATES)) == len(QUALITY_STATES)


def test_the_migration_and_the_application_agree_about_quality() -> None:
    """The migration rewrites stored quality values; the application reads the vocabulary. If the two
    disagree, a repaired row becomes an unreadable one."""

    import importlib.util
    import pathlib

    path = pathlib.Path(__file__).resolve().parents[2] / "alembic" / "versions" / "d5a71c93e2f8_telemetry_spine.py"
    spec = importlib.util.spec_from_file_location("telemetry_spine_migration", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert set(module.QUALITY_ALIASES.values()) <= set(QUALITY_STATES)


async def test_the_database_refuses_a_replayed_point(fabric: Fabric) -> None:
    org = fabric.org_id(fabric.alpha)
    async with fabric.session() as session:
        series = TimeSeries(
            id=new_id("tms"),
            org_id=org,
            well_id=fabric.alpha.well_id,
            scope_token=fabric.alpha.well_id,
            channel_key="spp",
            name="Standpipe pressure",
            dimension="pressure",
            unit=CANONICAL_UNIT["pressure"],
            source="manual",
        )
        session.add(series)
        await session.flush()
        digest = fingerprint(series_id=series.id, ts=INSTANT, value=3500.0, source_ref="csv")
        session.add(
            TimeSeriesPoint(
                id=new_id("tsp"),
                series_id=series.id,
                ts=INSTANT,
                value=3500.0,
                quality="good",
                received_at=INSTANT,
                fingerprint=digest,
                dedup_key=digest,
            )
        )
        await session.commit()
        series_id = series.id

    async with fabric.session() as session:
        session.add(
            TimeSeriesPoint(
                id=new_id("tsp"),
                series_id=series_id,
                ts=INSTANT,
                value=3500.0,
                quality="good",
                received_at=INSTANT,
                fingerprint=digest,
                dedup_key=digest,
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()


async def test_a_conflicting_reading_is_stored_beside_the_first_not_over_it(fabric: Fabric) -> None:
    """The database permits it — the identity is a fingerprint over the value — and the *service* is
    what reports the conflict. This test asserts the storage layer does not make the decision for it."""

    org = fabric.org_id(fabric.alpha)
    async with fabric.session() as session:
        series = TimeSeries(
            id=new_id("tms"),
            org_id=org,
            well_id=fabric.alpha.well_id,
            scope_token=fabric.alpha.well_id,
            channel_key="spp",
            name="Standpipe pressure",
            dimension="pressure",
            unit=CANONICAL_UNIT["pressure"],
            source="manual",
        )
        session.add(series)
        await session.flush()
        for value in (3500.0, 3625.0):
            digest = fingerprint(series_id=series.id, ts=INSTANT, value=value, source_ref="csv")
            session.add(
                TimeSeriesPoint(
                    id=new_id("tsp"),
                    series_id=series.id,
                    ts=INSTANT,
                    value=value,
                    quality="good",
                    received_at=INSTANT,
                    fingerprint=digest,
                    dedup_key=digest,
                )
            )
        await session.commit()
        series_id = series.id

    async with fabric.session() as session:
        rows = (
            (
                await session.execute(
                    select(TimeSeriesPoint)
                    .where(TimeSeriesPoint.series_id == series_id)
                    .order_by(TimeSeriesPoint.value)
                )
            )
            .scalars()
            .all()
        )
    assert [row.value for row in rows] == [3500.0, 3625.0]
