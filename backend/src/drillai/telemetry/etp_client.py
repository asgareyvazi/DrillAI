"""ETP 1.2 WebSocket subscription SourceAdapter (`etp.1.2.json_ws`).

Bridges ``EtpWebSocketClient`` to the platform ``SourceAdapter`` contract (`connect`, `describe_channels`,
`subscribe`, `poll`, `normalize`, `close`) so that both ``ConnectorService`` (`test_connection`, `preview`)
and the durable connector worker ingest ETP frames exclusively through ``TelemetryService``.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from drillai.core.errors import ValidationFailed
from drillai.telemetry.adapters import ChannelDescriptor, SourceFrame
from drillai.telemetry.connectors import parse_channel_mappings
from drillai.telemetry.protocols.etp import EtpChannelInfo, EtpPollBatch, EtpWebSocketClient
from drillai.telemetry.service import PointIn


class EtpSubscriptionAdapter:
    """ETP 1.2 JSON-framed WebSocket subscription adapter conforming to ``SourceAdapter``."""

    key = "etp.1.2.json_ws"
    source = "etp"

    def __init__(
        self,
        *,
        endpoint_url: str,
        channel_mappings: list[dict[str, Any]],
        config: dict[str, Any] | None = None,
        secrets: dict[str, str] | None = None,
        cursor: dict[str, Any] | None = None,
        well_id: str = "",
        wellbore_id: str | None = None,
        operation_id: str | None = None,
    ) -> None:
        self._endpoint_url = endpoint_url
        self._config = dict(config or {})
        self._config["channel_mappings"] = list(channel_mappings)
        self._mappings = parse_channel_mappings(channel_mappings)
        self._channels: dict[str, ChannelDescriptor] = {
            m.channel_key: ChannelDescriptor(
                channel_key=m.channel_key,
                name=m.name,
                dimension=m.dimension,
                unit=m.unit,
                description=m.description,
                is_realtime=m.is_realtime,
            )
            for m in self._mappings
        }
        self._secrets = dict(secrets or {})
        self._cursor: dict[str, Any] = dict(cursor or {})
        self._well_id = well_id
        self._wellbore_id = wellbore_id
        self._operation_id = operation_id
        self._client = EtpWebSocketClient(
            endpoint_url=endpoint_url,
            secrets=self._secrets,
            timeout_seconds=float(self._config.get("timeout_seconds", 10.0)),
        )
        self._connected = False
        self._subscribed: set[str] = set()
        self._discovered_info: list[EtpChannelInfo] = []
        self._last_batch: EtpPollBatch | None = None

    @property
    def cursor(self) -> dict[str, Any]:
        return dict(self._cursor)

    @property
    def last_batch(self) -> EtpPollBatch | None:
        return self._last_batch

    async def connect(self) -> None:
        max_pts = int(self._config.get("max_points_per_poll", 250))
        self._last_batch = await self._client.poll_subscription_batch(
            well_id=self._well_id or "well-probe",
            wellbore_id=self._wellbore_id,
            operation_id=self._operation_id,
            config=self._config,
            watermark=self._cursor,
            max_frames=max_pts,
        )
        self._discovered_info = list(self._last_batch.discovered_channels)
        self._connected = True

    async def describe_channels(self) -> Sequence[ChannelDescriptor]:
        if not self._connected:
            await self.connect()
        discovered_units = {
            ch.mnemonic.upper(): ch.unit
            for ch in self._discovered_info
            if ch.unit
        }
        result: list[ChannelDescriptor] = []
        for m in self._mappings:
            unit = discovered_units.get(m.source_mnemonic.upper()) or m.unit
            desc = ChannelDescriptor(
                channel_key=m.channel_key,
                name=m.name,
                dimension=m.dimension,
                unit=unit,
                description=m.description,
                is_realtime=m.is_realtime,
            )
            self._channels[m.channel_key] = desc
            result.append(desc)
        return result

    async def subscribe(self, channel_keys: Sequence[str]) -> None:
        unknown = {k for k in channel_keys if k not in self._channels}
        if unknown:
            raise ValidationFailed(
                "cannot subscribe to channels the ETP adapter does not describe",
                details={"unknown": sorted(unknown)},
            )
        self._subscribed = set(channel_keys)

    async def poll(self) -> Sequence[SourceFrame]:
        if not self._connected:
            await self.connect()
        max_pts = int(self._config.get("max_points_per_poll", 250))
        if self._last_batch is not None:
            batch = self._last_batch
            self._last_batch = None
        else:
            batch = await self._client.poll_subscription_batch(
                well_id=self._well_id or "well-probe",
                wellbore_id=self._wellbore_id,
                operation_id=self._operation_id,
                config=self._config,
                watermark=self._cursor,
                max_frames=max_pts,
            )
            self._discovered_info = list(batch.discovered_channels)
        self._cursor = dict(batch.next_watermark)
        frames = [
            f
            for f in batch.frames
            if not self._subscribed or f.channel_key in self._subscribed
        ]
        return frames[:max_pts]

    def normalize(self, frame: SourceFrame) -> PointIn:
        frame.validate()
        descriptor = self._channels.get(frame.channel_key)
        if descriptor is None:
            raise ValidationFailed(
                f"ETP adapter produced a frame for an undescribed channel {frame.channel_key!r}",
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
        self._last_batch = None
