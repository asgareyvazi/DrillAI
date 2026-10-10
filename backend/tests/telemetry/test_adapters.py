"""The adapter boundary: a foreign protocol becomes a platform measurement exactly once.

What is asserted here is the contract and the refusals. The contract, because a connector written
against a different shape must still land in the same ingestion path — describe, subscribe, poll,
normalize, and let the *service* convert units and detect replays. The refusals, because a boundary that
coerces is a boundary that hides: a timestamp without a timezone, a mnemonic the channel set does not
contain, a unit the dimension cannot convert and a non-numeric value must all stop the frame instead of
being repaired into something plausible.

Wire-level SOAP/ETP interop is **not** claimed here; the WITSML-shaped adapter consumes the documented
JSON projection of a channel subscription, which is what a connector hands the platform.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import select

from drillai.core.errors import ValidationFailed
from drillai.db.models import Organization, Project, TimeSeries, Well
from drillai.telemetry.adapters import (
    ChannelDescriptor,
    SourceAdapter,
    SourceFrame,
    SyntheticAdapter,
    WitsmlShapedAdapter,
    ingest_frames,
    validate_descriptor,
)
from drillai.telemetry.service import TelemetryService

ORG = "org_adapters"
WELL = "well_adapters"
START = dt.datetime(2026, 3, 15, 8, 0, tzinfo=dt.UTC)


async def _tenant(session) -> None:
    session.add(
        Organization(
            id=ORG,
            slug="adapters",
            name="Adapters Co",
            kind="operator",
            timezone="UTC",
            default_unit_system="metric",
            default_locale="en",
            is_active=True,
        )
    )
    await session.flush()
    session.add(
        Project(id="prj_adapters", org_id=ORG, name="Adapters", status="active", datum_policy="rkb", settings={})
    )
    await session.flush()
    session.add(
        Well(
            id=WELL,
            org_id=ORG,
            project_id="prj_adapters",
            name="AD-1",
            well_type="development_producer",
            elevation_datum="msl",
            is_offshore=False,
            twin_state="none",
            tags=[],
            attributes={},
            is_demo_fixture=False,
        )
    )
    await session.flush()


def _spp() -> ChannelDescriptor:
    return ChannelDescriptor(channel_key="spp", name="Standpipe pressure", dimension="pressure", unit="psi")


# --------------------------------------------------------------------------- the contract


async def test_a_synthetic_adapter_satisfies_the_protocol_and_is_deterministic(session) -> None:
    adapter = SyntheticAdapter(
        [_spp()],
        {"spp": [(0.0, 3500.0), (1.0, 3520.0), (2.0, 3600.0)]},
        start=START,
    )
    assert isinstance(adapter, SourceAdapter)

    await adapter.connect()
    descriptors = await adapter.describe_channels()
    assert [descriptor.channel_key for descriptor in descriptors] == ["spp"]
    assert descriptors[0].dimension == "pressure"

    batches = []
    for _ in range(5):
        frames = await adapter.poll()
        if not frames:
            break
        batches.append([(frame.channel_key, frame.ts, frame.value) for frame in frames])
    await adapter.close()

    assert batches == [
        [("spp", START, 3500.0)],
        [("spp", START + dt.timedelta(seconds=1), 3520.0)],
        [("spp", START + dt.timedelta(seconds=2), 3600.0)],
    ], "the plan is released in order, once, at the caller's pace"

    # A second run of the same plan produces the same frames: no clock, no randomness.
    again = SyntheticAdapter([_spp()], {"spp": [(0.0, 3500.0), (1.0, 3520.0), (2.0, 3600.0)]}, start=START)
    await again.connect()
    repeats = [[(frame.ts, frame.value) for frame in await again.poll()]]
    assert repeats == [[(START, 3500.0)]]


async def test_polling_before_connecting_is_refused(session) -> None:
    adapter = SyntheticAdapter([_spp()], {"spp": [(0.0, 1.0)]})
    with pytest.raises(ValidationFailed) as failure:
        await adapter.poll()
    assert "connect" in failure.value.details["field"]


async def test_a_plan_naming_an_undescribed_channel_is_refused_at_construction() -> None:
    with pytest.raises(ValidationFailed) as failure:
        SyntheticAdapter([_spp()], {"wob": [(0.0, 1.0)]})
    assert failure.value.details["unknown"] == ["wob"]


async def test_subscribing_to_an_undescribed_channel_is_refused() -> None:
    adapter = SyntheticAdapter([_spp()], {"spp": [(0.0, 1.0)]})
    await adapter.connect()
    with pytest.raises(ValidationFailed):
        await adapter.subscribe(["wob"])


# --------------------------------------------------------------------------- frame refusals


def test_a_frame_without_a_timezone_cannot_be_placed_in_time() -> None:
    frame = SourceFrame(channel_key="spp", ts=dt.datetime(2026, 3, 15, 8, 0), value=1.0, unit="psi")
    with pytest.raises(ValidationFailed) as failure:
        frame.validate()
    assert failure.value.details["field"] == "ts"


def test_a_frame_with_an_unknown_quality_is_refused() -> None:
    frame = SourceFrame(channel_key="spp", ts=START, value=1.0, unit="psi", quality="probably fine")
    with pytest.raises(ValidationFailed) as failure:
        frame.validate()
    assert failure.value.details["field"] == "quality"
    assert "probably fine" in failure.value.details["value"]


def test_a_frame_value_that_is_not_a_number_is_refused() -> None:
    frame = SourceFrame(channel_key="spp", ts=START, value="3500", unit="psi")
    with pytest.raises(ValidationFailed) as failure:
        frame.validate()
    assert failure.value.details["field"] == "value"
    assert failure.value.details["type"] == "str"


def test_an_absent_value_is_allowed_and_stays_absent() -> None:
    """``None`` is an explicit absence, not zero — the platform stores it as missing quality."""

    frame = SourceFrame(channel_key="spp", ts=START, value=None, unit="psi", quality="missing")
    frame.validate()


# --------------------------------------------------------------------------- WITSML-shaped frames


def _witsml(adapter_frames: list[dict]) -> WitsmlShapedAdapter:
    return WitsmlShapedAdapter([_spp()], adapter_frames)


async def test_witsml_shaped_frames_are_decoded_with_their_own_unit_and_identity() -> None:
    adapter = _witsml(
        [
            {
                "channel": {
                    "mnemonic": "SPP",
                    "uom": "psi",
                    "value": "3412.5",
                    "quality": "good",
                    "index": 7,
                    "dTim": "2026-03-15T08:00:00+00:00",
                }
            }
        ]
    )
    await adapter.connect()
    frames = await adapter.poll()
    assert len(frames) == 1
    frame = frames[0]
    assert frame.channel_key == "spp", "the mnemonic is lower-cased to the platform's key"
    assert frame.value == pytest.approx(3412.5)
    assert frame.unit == "psi"
    assert frame.source_point_id == "witsml:spp:7"
    point = adapter.normalize(frame)
    assert point.value == pytest.approx(3412.5)
    assert point.unit == "psi", "the adapter does not convert; the service does"


async def test_a_witsml_frame_for_an_unknown_mnemonic_is_refused_by_name() -> None:
    adapter = _witsml(
        [{"channel": {"mnemonic": "XYZ", "uom": "psi", "value": 1.0, "dTim": "2026-03-15T08:00:00+00:00"}}]
    )
    await adapter.connect()
    with pytest.raises(ValidationFailed) as failure:
        await adapter.poll()
    assert failure.value.details["field"] == "mnemonic"
    assert failure.value.details["value"] == "XYZ"


async def test_a_witsml_frame_in_a_unit_the_dimension_cannot_convert_is_refused() -> None:
    adapter = _witsml(
        [{"channel": {"mnemonic": "SPP", "uom": "rpm", "value": 1.0, "dTim": "2026-03-15T08:00:00+00:00"}}]
    )
    await adapter.connect()
    with pytest.raises(ValidationFailed) as failure:
        await adapter.poll()
    assert failure.value.details["field"] == "unit"


@pytest.mark.parametrize("value", ["high", float("nan"), float("inf")])
async def test_a_witsml_frame_whose_value_is_not_finite_is_refused(value) -> None:
    adapter = _witsml(
        [{"channel": {"mnemonic": "SPP", "uom": "psi", "value": value, "dTim": "2026-03-15T08:00:00+00:00"}}]
    )
    await adapter.connect()
    with pytest.raises(ValidationFailed):
        await adapter.poll()


async def test_a_witsml_frame_without_an_offset_is_refused() -> None:
    adapter = _witsml(
        [{"channel": {"mnemonic": "SPP", "uom": "psi", "value": 1.0, "dTim": "2026-03-15T08:00:00"}}]
    )
    await adapter.connect()
    with pytest.raises(ValidationFailed) as failure:
        await adapter.poll()
    assert failure.value.details["field"] == "dTim"


def test_a_descriptor_the_platform_cannot_register_is_refused() -> None:
    with pytest.raises(ValidationFailed) as failure:
        validate_descriptor(
            ChannelDescriptor(channel_key="x", name="X", dimension="loudness", unit="dB")
        )
    assert failure.value.details["field"] == "dimension"

    with pytest.raises(ValidationFailed):
        validate_descriptor(ChannelDescriptor(channel_key="x", name="X", dimension="pressure", unit="rpm"))


# --------------------------------------------------------------------------- through the service


async def test_ingest_frames_stores_canonical_values_with_provenance(session) -> None:
    await _tenant(session)
    adapter = WitsmlShapedAdapter(
        [_spp()],
        [
            {
                "channel": {
                    "mnemonic": "SPP",
                    "uom": "psi",
                    "value": "3412.5",
                    "index": 1,
                    "dTim": "2026-03-15T08:00:00+00:00",
                }
            },
            {
                "channel": {
                    "mnemonic": "SPP",
                    "uom": "psi",
                    "value": "3510.0",
                    "index": 2,
                    "dTim": "2026-03-15T08:00:20+00:00",
                }
            },
        ],
    )
    service = TelemetryService(session, ORG)
    report = await ingest_frames(service, adapter, well_id=WELL)
    await session.commit()

    payload = report.to_dict()
    assert payload["channels_created"] == 1
    assert payload["frames"] == 2
    assert payload["totals"] == {"received": 2, "accepted": 2, "duplicates": 0, "rejected": 0}
    assert payload["per_channel"]["spp"]["accepted"] == 2

    channel = (await session.execute(select(TimeSeries).where(TimeSeries.org_id == ORG))).scalars().one()
    assert channel.unit == "Pa", "the stored unit is canonical"
    assert channel.src_unit == "psi", "and the source's unit is kept as provenance"
    points = (
        (
            await session.execute(
                select(TimeSeriesPointRow.ts, TimeSeriesPointRow.value, TimeSeriesPointRow.src_unit, TimeSeriesPointRow.source_point_id)
                .where(TimeSeriesPointRow.series_id == channel.id)
                .order_by(TimeSeriesPointRow.ts)
            )
        )
        .all()
    )
    assert points[0].value == pytest.approx(3412.5 * 6894.757293168)
    assert points[1].value == pytest.approx(3510.0 * 6894.757293168)
    assert {point.src_unit for point in points} == {"psi"}
    assert {point.source_point_id for point in points} == {"witsml:spp:1", "witsml:spp:2"}


async def test_ingesting_the_same_frames_twice_is_a_replay_not_a_second_row(session) -> None:
    """Replayed frames carry the same source identity, so the platform recognises them — the property
    that makes a connector's at-least-once delivery safe."""

    await _tenant(session)
    documents = [
        {
            "channel": {
                "mnemonic": "SPP",
                "uom": "psi",
                "value": "3412.5",
                "index": 1,
                "dTim": "2026-03-15T08:00:00+00:00",
            }
        }
    ]
    service = TelemetryService(session, ORG)
    first = await ingest_frames(service, WitsmlShapedAdapter([_spp()], documents), well_id=WELL)
    await session.commit()
    second = await ingest_frames(service, WitsmlShapedAdapter([_spp()], documents), well_id=WELL)
    await session.commit()

    assert first.to_dict()["totals"]["accepted"] == 1
    assert second.to_dict()["totals"]["accepted"] == 0
    assert second.to_dict()["totals"]["duplicates"] == 1

    channel = (await session.execute(select(TimeSeries).where(TimeSeries.org_id == ORG))).scalars().one()
    rows = (
        (
            await session.execute(
                select(TimeSeriesPointRow).where(TimeSeriesPointRow.series_id == channel.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1


from drillai.core.errors import Conflict
from drillai.db.models import Alert, OutboxEvent
from drillai.db.models import TimeSeriesPoint as TimeSeriesPointRow
from drillai.telemetry.adapters import AdapterRunner
from drillai.telemetry.alerts import AlertRuleService


async def test_adapter_runner_drives_synthetic_adapter_through_telemetry_and_auto_alerts(session) -> None:
    """AdapterRunner executes SyntheticAdapter -> TelemetryService -> AlertService -> Outbox in one
    governed path and reports accurate runtime health."""

    import contextlib

    await _tenant(session)
    await AlertRuleService(session, ORG).create(
        rule_key="spp-high-runner",
        name="SPP High Runner",
        channel_key="spp",
        operator="gt",
        threshold=4000.0,
        unit="psi",
        severity="high",
        sustain_seconds=5.0,
        cooldown_seconds=0.0,
        well_id=WELL,
    )
    await session.commit()

    adapter = SyntheticAdapter(
        [_spp()],
        {"spp": [(0.0, 4150.0), (5.0, 4250.0)]},
        start=START,
    )

    @contextlib.asynccontextmanager
    async def _borrowed():
        yield session

    runner = AdapterRunner(
        session_factory=_borrowed,
        org_id=ORG,
        well_id=WELL,
        adapter=adapter,
    )
    assert runner.health().status == "stopped"
    assert runner.health().is_live is False

    await runner.initialize()
    step1 = await runner.step()
    assert step1.frames == 1
    assert step1.alerts_raised == 0
    step2 = await runner.step()
    assert step2.frames == 1
    assert step2.alerts_raised == 1
    assert runner.health().status == "running"
    assert runner.health().is_live is True
    assert runner.health().points_accepted == 2
    assert runner.health().alerts_raised == 1

    await runner.stop()
    assert runner.health().status == "stopped"
    assert runner.health().is_live is False

    alerts = (
        await session.execute(select(Alert).where(Alert.org_id == ORG, Alert.well_id == WELL))
    ).scalars().all()
    assert len(alerts) == 1
    assert alerts[0].status == "raised"

    outbox_types = (
        await session.execute(
            select(OutboxEvent.event_type).where(OutboxEvent.org_id == ORG, OutboxEvent.well_id == WELL)
        )
    ).scalars().all()
    assert "telemetry.received" in outbox_types
    assert "alert.raised" in outbox_types


async def test_adapter_runner_prevents_duplicate_start_and_tracks_failure_backoff(session) -> None:
    """Starting an already-running AdapterRunner is refused with Conflict; poll failures transition
    through backing_off to failed and never claim is_live=True."""

    import contextlib

    await _tenant(session)

    @contextlib.asynccontextmanager
    async def _borrowed():
        yield session

    runner = AdapterRunner(
        session_factory=_borrowed,
        org_id=ORG,
        well_id=WELL,
        adapter=SyntheticAdapter([_spp()], {"spp": [(0.0, 3500.0)]}, start=START),
        poll_interval_seconds=0.5,
    )
    await runner.start()
    try:
        with pytest.raises(Conflict):
            await runner.start()
    finally:
        await runner.stop()
    assert runner.health().status == "stopped"
    assert runner.health().is_live is False

    class _FailingAdapter(SyntheticAdapter):
        async def poll(self):
            raise RuntimeError("upstream socket reset")

    failing_runner = AdapterRunner(
        session_factory=_borrowed,
        org_id=ORG,
        well_id=WELL,
        adapter=_FailingAdapter([_spp()], {"spp": [(0.0, 3500.0)]}, start=START),
        base_backoff_seconds=0.05,
        max_backoff_seconds=0.5,
        max_consecutive_failures=2,
    )
    await failing_runner.initialize()
    with pytest.raises(RuntimeError, match="upstream socket reset"):
        await failing_runner.step()
    h1 = failing_runner.health()
    assert h1.status == "backing_off"
    assert h1.is_live is False
    assert h1.consecutive_failures == 1
    assert h1.backoff_seconds == pytest.approx(0.05)
    assert "upstream socket reset" in (h1.last_error or "")

    with pytest.raises(RuntimeError, match="upstream socket reset"):
        await failing_runner.step()
    h2 = failing_runner.health()
    assert h2.status == "failed"
    assert h2.is_live is False
    assert h2.consecutive_failures == 2

