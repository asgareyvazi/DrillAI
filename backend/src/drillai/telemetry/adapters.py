"""Source adapters: the only place a foreign protocol becomes a platform measurement.

Everything upstream of this module speaks somebody else's shape — a WITSML channel set, an ETP channel
subscription, a CSV the night shift exported, a simulated rig. Everything downstream speaks the
platform's: a channel key, a canonical dimension, a value in the canonical unit, a quality, a source
identity and a timestamp with a timezone. This module is the boundary, and it is deliberately the *only*
one: an adapter that normalized units itself would be a second unit engine, and the mission's rule is
that there is exactly one.

What a real integration does with these classes is out of scope and is **not** claimed here. The contract
(``connect`` / ``describe_channels`` / ``subscribe`` / ``poll`` / ``normalize`` / ``close``) is exercised
against two implementations that a deployment can run today:

* :class:`SyntheticAdapter` — deterministic frames from a declared plan. No randomness, no wall clock: a
  replay of the same plan produces the same measurements, which is what makes it usable in tests and in a
  commissioning dry-run. (This is also the answer to "no fake live data": the synthetic source is a real
  adapter with a declared source kind, not a stub hidden inside a service.)
* :class:`WitsmlShapedAdapter` — the *documented JSON projection* of a WITSML/ETP channel frame. It
  demonstrates how a connector's frames are turned into platform points, including the refusals: a
  mnemonics it does not know, a unit its dimension cannot convert, a timestamp without a timezone, and a
  non-numeric value all stop the frame rather than being coerced. Wire-level SOAP/ETP interop is **not**
  certified by this module — see the report's limitations.

Adapters never write to the database. They produce frames; :func:`ingest_frames` hands those frames to
``TelemetryService``, which is where the channel resolution, unit conversion, replay detection and audit
live. An adapter that could write would be a second ingestion path with its own rules.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from drillai.core.errors import ValidationFailed
from drillai.telemetry.service import IngestReport, PointIn, TelemetryService
from drillai.telemetry.units import convert
from drillai.telemetry.vocabulary import CHANNEL_DIMENSIONS, QUALITY_STATES, SOURCE_KINDS

__all__ = [
    "ChannelDescriptor",
    "SourceAdapter",
    "SourceFrame",
    "SyntheticAdapter",
    "WitsmlShapedAdapter",
    "ingest_frames",
]

#: How many frames one ``poll`` returns, whatever the source has queued. A source that floods a poll is
#: bounded here rather than in the database, so the memory a connector needs does not grow with the rig's
#: data rate.
MAX_FRAMES_PER_POLL = 500


@dataclass(frozen=True)
class ChannelDescriptor:
    """What a source says about one channel before any value arrives.

    The dimension is the source's claim about what the quantity *is*, and it is declared up front
    because that is what makes the unit checkable: a value in ``psi`` on a channel declared as a
    pressure is a conversion; the same value on a channel declared as a rotary speed is a
    mis-configuration, and it is refused rather than converted.
    """

    channel_key: str
    name: str
    dimension: str
    unit: str
    description: str | None = None
    is_realtime: bool = True


@dataclass(frozen=True)
class SourceFrame:
    """One measurement as it arrived, before normalization.

    ``source_ref`` identifies where in the source it came from (a frame index, a document id, a request
    id) and is kept on the stored point: two points with the same timestamp and different values are
    otherwise indistinguishable, and "which reading is the real one?" is a question the platform must be
    able to answer.
    """

    channel_key: str
    ts: dt.datetime
    value: float | None
    unit: str | None = None
    quality: str = "good"
    source_point_id: str | None = None
    source_ref: str | None = None
    sequence: int | None = None
    depth_md_si: float | None = None

    def validate(self) -> None:
        """Refuse a frame the platform cannot store, naming the field.

        These are the checks a protocol frame needs before it becomes a measurement: a timestamp that
        cannot be placed in time, a quality outside the closed vocabulary, a value that is neither a
        number nor an explicit absence.
        """

        if not self.channel_key or not self.channel_key.strip():
            raise ValidationFailed("the frame has no channel key", details={"field": "channel_key"})
        if self.ts.tzinfo is None or self.ts.utcoffset() is None:
            raise ValidationFailed(
                "the frame's timestamp has no timezone, so it cannot be placed in time",
                details={"field": "ts", "value": self.ts.isoformat()},
            )
        if self.quality not in QUALITY_STATES:
            raise ValidationFailed(
                "the frame's quality is not one the platform records",
                details={"field": "quality", "value": self.quality, "allowed": list(QUALITY_STATES)},
            )
        if self.value is not None and not isinstance(self.value, int | float):
            raise ValidationFailed(
                "the frame's value is neither a number nor an explicit absence",
                details={"field": "value", "type": type(self.value).__name__},
            )


@runtime_checkable
class SourceAdapter(Protocol):
    """The contract every source implements: connect, describe, subscribe, poll, normalize, close."""

    key: str
    source: str

    async def connect(self) -> None: ...

    async def describe_channels(self) -> Sequence[ChannelDescriptor]: ...

    async def subscribe(self, channel_keys: Sequence[str]) -> None: ...

    async def poll(self) -> Sequence[SourceFrame]: ...

    def normalize(self, frame: SourceFrame) -> PointIn: ...

    async def close(self) -> None: ...


class SyntheticAdapter:
    """A deterministic source: frames come from a plan, not from a clock or a random number generator.

    The plan is a list of ``(offset_seconds, value)`` pairs per channel, and each ``poll`` returns the
    frames whose offset has been passed since the last poll *relative to the adapter's own cursor*, not
    to wall time. A caller can therefore drive a whole well's worth of telemetry through the real
    ingestion path at whatever speed it likes and get exactly the same measurements every run.
    """

    key = "synthetic"
    source = "synthetic"

    def __init__(
        self,
        channels: Iterable[ChannelDescriptor],
        plan: dict[str, Sequence[tuple[float, float | None]]],
        *,
        start: dt.datetime | None = None,
        quality: str = "good",
    ) -> None:
        self._channels = {channel.channel_key: channel for channel in channels}
        unknown = {key for key in plan if key not in self._channels}
        if unknown:
            raise ValidationFailed(
                "the plan names channels the adapter does not describe",
                details={"unknown": sorted(unknown), "described": sorted(self._channels)},
            )
        self._plan = {key: list(entries) for key, entries in plan.items()}
        self._start = start or dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
        self._quality = quality
        #: The plan offset the adapter has released so far. The plan is in seconds relative to
        #: ``start``; the cursor is deliberately *not* wall-clock time, so a caller controls the pace.
        self._cursor: float | None = None
        self._index: dict[str, int] = dict.fromkeys(self._plan, 0)
        self._connected = False
        self._subscribed: set[str] = set()

    # ------------------------------------------------------------------ contract
    async def connect(self) -> None:
        self._connected = True
        self._cursor = None
        self._index = dict.fromkeys(self._plan, 0)

    async def describe_channels(self) -> Sequence[ChannelDescriptor]:
        return list(self._channels.values())

    async def subscribe(self, channel_keys: Sequence[str]) -> None:
        unknown = {key for key in channel_keys if key not in self._channels}
        if unknown:
            raise ValidationFailed(
                "cannot subscribe to channels the adapter does not describe",
                details={"unknown": sorted(unknown)},
            )
        self._subscribed = set(channel_keys)

    async def poll(self) -> Sequence[SourceFrame]:
        """The next due batch of frames from the plan — never a wall-clock slice of it.

        Each poll releases every frame whose planned offset is at or before the adapter's cursor, and
        then moves the cursor to the next offset that has not been released yet. So a caller that polls
        until it gets an empty list has drained the plan exactly once, in plan order, at whatever speed
        it liked — which is what makes the synthetic source usable both as a test fixture and as a
        commissioning dry-run.
        """

        if not self._connected:
            raise ValidationFailed(
                f"adapter {self.key!r} was asked to poll before it connected", details={"field": "connect"}
            )
        pending: list[tuple[float, str, int]] = []
        for channel_key, entries in self._plan.items():
            if self._subscribed and channel_key not in self._subscribed:
                continue
            index = self._index[channel_key]
            if index < len(entries):
                pending.append((entries[index][0], channel_key, index))
        if not pending:
            return []
        if self._cursor is None:
            self._cursor = min(offset for offset, _key, _index in pending)

        frames: list[SourceFrame] = []
        for offset, channel_key, index in sorted(pending):
            if offset > self._cursor or len(frames) >= MAX_FRAMES_PER_POLL:
                continue
            value = self._plan[channel_key][index][1]
            frames.append(
                SourceFrame(
                    channel_key=channel_key,
                    ts=self._start + dt.timedelta(seconds=offset),
                    value=value,
                    unit=self._channels[channel_key].unit,
                    quality=self._quality,
                    source_point_id=f"{self.key}:{channel_key}:{index}",
                    source_ref=f"plan[{index}]",
                    sequence=index,
                )
            )
            self._index[channel_key] = index + 1
        remaining = [
            entries[self._index[key]][0]
            for key, entries in self._plan.items()
            if self._index[key] < len(entries)
        ]
        self._cursor = min(remaining) if remaining else None
        if not frames:  # pragma: no cover - defensive: pending implies a release at the cursor
            return []
        return frames

    def normalize(self, frame: SourceFrame) -> PointIn:
        """The platform's point: the adapter may re-label, never re-scale.

        The value and the unit are passed through untouched, so the central engine performs the single
        conversion from the source unit to the canonical one. An adapter that multiplied here would be a
        second unit engine, and two engines disagree eventually.
        """

        frame.validate()
        descriptor = self._channels.get(frame.channel_key)
        if descriptor is None:
            raise ValidationFailed(
                f"adapter {self.key!r} produced a frame for an undescribed channel",
                details={"channel_key": frame.channel_key},
            )
        return PointIn(
            ts=frame.ts,
            value=frame.value,
            unit=frame.unit or descriptor.unit,
            quality=frame.quality,
            source_point_id=frame.source_point_id,
            source_ref=frame.source_ref,
            sequence=frame.sequence,
            depth_md_si=frame.depth_md_si,
        )

    async def close(self) -> None:
        self._connected = False


class WitsmlShapedAdapter:
    """A connector's frames in the JSON projection of a WITSML/ETP channel subscription.

    The shape this accepts is documented here because it is the contract between a connector and the
    platform::

        {
          "channel": {
            "mnemonic": "SPP",          # the source's channel key
            "uom": "psi",               # the unit the value is in
            "value": "3412.5",          # numeric, or null for an explicit absence
            "quality": "good",          # optional; defaults to good
            "index": 12,                # optional sequence within the subscription
            "dTim": "2026-03-15T08:00:00+00:00"  # required, with an offset
          }
        }

    ``describe_channels`` is fed from the same channel set the subscription was made against, so a
    mnemonic the set does not contain is refused by name rather than invented. The values are strings in
    WITSML; they are parsed to floats **once**, here, and a value that is not numeric is refused instead
    of becoming ``NaN``.
    """

    key = "witsml_shaped"
    source = "witsml"

    def __init__(
        self,
        channels: Iterable[ChannelDescriptor],
        frames: Iterable[dict[str, Any]],
        *,
        source_ref: str = "witsml:channel-set",
    ) -> None:
        self._channels = {channel.channel_key: channel for channel in channels}
        self._frames = [dict(item) for item in frames]
        self._source_ref = source_ref
        self._connected = False
        self._subscribed: set[str] = set()

    async def connect(self) -> None:
        self._connected = True

    async def describe_channels(self) -> Sequence[ChannelDescriptor]:
        return list(self._channels.values())

    async def subscribe(self, channel_keys: Sequence[str]) -> None:
        self._subscribed = set(channel_keys)

    async def poll(self) -> Sequence[SourceFrame]:
        if not self._connected:
            raise ValidationFailed(f"adapter {self.key!r} was asked to poll before it connected")
        remaining = self._frames
        self._frames = []
        frames: list[SourceFrame] = []
        for position, document in enumerate(remaining[:MAX_FRAMES_PER_POLL]):
            frame = self._decode(document, position)
            if self._subscribed and frame.channel_key not in self._subscribed:
                continue
            frames.append(frame)
        return frames

    async def close(self) -> None:
        self._connected = False

    def normalize(self, frame: SourceFrame) -> PointIn:
        frame.validate()
        return PointIn(
            ts=frame.ts,
            value=frame.value,
            unit=frame.unit,
            quality=frame.quality,
            source_point_id=frame.source_point_id,
            source_ref=frame.source_ref,
            sequence=frame.sequence,
        )

    # ------------------------------------------------------------------ decoding
    def _decode(self, document: dict[str, Any], position: int) -> SourceFrame:
        channel = document.get("channel")
        if not isinstance(channel, dict):
            raise ValidationFailed(
                "the frame has no channel object", details={"field": "channel", "position": position}
            )
        mnemonic = channel.get("mnemonic")
        if not isinstance(mnemonic, str) or not mnemonic.strip():
            raise ValidationFailed(
                "the frame has no channel mnemonic",
                details={"field": "mnemonic", "position": position},
            )
        key = mnemonic.strip().lower()
        descriptor = self._channels.get(key)
        if descriptor is None:
            raise ValidationFailed(
                "the frame names a channel this adapter does not describe",
                details={"field": "mnemonic", "value": mnemonic, "position": position},
            )
        uom = channel.get("uom") or descriptor.unit
        # The unit is validated here so a frame with an unusable unit does not become a stored point:
        # the conversion itself happens once, in the service, but *whether it can happen at all* is
        # answered at the boundary where the source's claim is still visible.
        convert(0.0, uom, descriptor.dimension)
        raw_value = channel.get("value")
        if raw_value is None:
            value: float | None = None
        else:
            try:
                value = float(raw_value)
            except (TypeError, ValueError) as exc:
                raise ValidationFailed(
                    "the frame's value is not numeric",
                    details={"field": "value", "value": raw_value, "position": position},
                ) from exc
            if value != value or value in (float("inf"), float("-inf")):  # NaN or ±inf
                raise ValidationFailed(
                    "the frame's value is not a finite measurement",
                    details={"field": "value", "value": raw_value, "position": position},
                )
        stamp = channel.get("dTim")
        ts = _parse_timestamp(stamp, position=position)
        quality = channel.get("quality") or "good"
        index = channel.get("index")
        return SourceFrame(
            channel_key=key,
            ts=ts,
            value=value,
            unit=uom,
            quality=str(quality).lower(),
            source_point_id=f"{self.source}:{key}:{index if index is not None else position}",
            source_ref=self._source_ref,
            sequence=int(index) if isinstance(index, int) else None,
        )


def _parse_timestamp(value: Any, *, position: int) -> dt.datetime:
    if not isinstance(value, str):
        raise ValidationFailed(
            "the frame has no timestamp",
            details={"field": "dTim", "position": position, "value": value},
        )
    try:
        stamp = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationFailed(
            "the frame's timestamp is not ISO-8601",
            details={"field": "dTim", "value": value, "position": position},
        ) from exc
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValidationFailed(
            "the frame's timestamp has no timezone offset, so it cannot be placed in time",
            details={"field": "dTim", "value": value, "position": position},
        )
    return stamp


@dataclass
class AdapterIngestReport:
    """What one ingestion run over an adapter did, per channel and in total."""

    adapter: str
    source: str
    channels_created: int = 0
    reports: dict[str, IngestReport] = field(default_factory=dict)
    frames: int = 0

    def to_dict(self) -> dict[str, Any]:
        totals = {
            "received": sum(report.received for report in self.reports.values()),
            "accepted": sum(report.accepted for report in self.reports.values()),
            "duplicates": sum(report.duplicates for report in self.reports.values()),
            "rejected": sum(report.rejected for report in self.reports.values()),
        }
        return {
            "adapter": self.adapter,
            "source": self.source,
            "frames": self.frames,
            "channels": sorted(self.reports),
            "channels_created": self.channels_created,
            "totals": totals,
            "per_channel": {key: report.to_dict() for key, report in sorted(self.reports.items())},
        }


async def ingest_frames(
    service: TelemetryService,
    adapter: SourceAdapter,
    *,
    well_id: str,
    wellbore_id: str | None = None,
    operation_id: str | None = None,
    subscribe: Sequence[str] | None = None,
    close: bool = True,
) -> AdapterIngestReport:
    """Connect, describe, subscribe, poll once, normalize and store — in that order, once.

    A poll is one transaction's worth of work: the frames are grouped by channel, each group is appended
    through ``TelemetryService`` (which is where conversion, replay detection and the audit row live),
    and the report says per channel what happened. The adapter is *not* allowed to write; it only
    produces frames, which is what keeps ingestion's guarantees in one place.
    """

    if adapter.source not in SOURCE_KINDS:
        raise ValidationFailed(
            "the adapter's source kind is not one the platform records",
            details={"field": "source", "value": adapter.source, "allowed": list(SOURCE_KINDS)},
        )
    report = AdapterIngestReport(adapter=adapter.key, source=adapter.source)
    await adapter.connect()
    try:
        descriptors = list(await adapter.describe_channels())
        if subscribe is not None:
            await adapter.subscribe(subscribe)
        # Channels are registered through the service, one per descriptor the source declares — the
        # descriptor is the source's claim about dimension and unit, and the service is where that claim
        # is validated against the unit table.
        resolved: dict[str, str] = {}
        for descriptor in descriptors:
            channel, created = await service.create_channel(
                well_id=well_id,
                wellbore_id=wellbore_id,
                operation_id=operation_id,
                channel_key=descriptor.channel_key,
                name=descriptor.name,
                dimension=descriptor.dimension,
                unit=descriptor.unit,
                description=descriptor.description,
                is_realtime=descriptor.is_realtime,
                source=adapter.source,
            )
            resolved[descriptor.channel_key] = channel.id
            if created:
                report.channels_created += 1
        frames = list(await adapter.poll())
        report.frames = len(frames)
        grouped: dict[str, list[PointIn]] = {}
        for frame in frames:
            point = adapter.normalize(frame)
            key = frame.channel_key.strip().lower()
            if key not in resolved:
                raise ValidationFailed(
                    "the source produced a frame for a channel it did not describe",
                    details={"channel_key": frame.channel_key, "described": sorted(resolved)},
                )
            grouped.setdefault(key, []).append(point)
        for key, points in grouped.items():
            report.reports[key] = await service.append_points(resolved[key], points)
        return report
    finally:
        if close:
            await adapter.close()


def validate_descriptor(descriptor: ChannelDescriptor) -> None:
    """A descriptor the platform cannot register is refused before any value arrives."""

    if descriptor.dimension not in CHANNEL_DIMENSIONS:
        raise ValidationFailed(
            "the channel's dimension is not one the platform stores",
            details={
                "field": "dimension",
                "value": descriptor.dimension,
                "allowed": sorted(CHANNEL_DIMENSIONS),
            },
        )
    convert(0.0, descriptor.unit, descriptor.dimension)
