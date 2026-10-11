"""WITSML 1.4.1.1 SOAP polling SourceAdapter (`witsml.1.4.1.1.soap_http`).

Bridges ``WitsmlSoapClient`` to the platform ``SourceAdapter`` contract (`connect`, `describe_channels`,
`subscribe`, `poll`, `normalize`, `close`) so that both ``ConnectorService`` (`test_connection`, `preview`)
and the durable connector worker ingest WITSML frames exclusively through ``TelemetryService``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from drillai.core.errors import ValidationFailed
from drillai.telemetry.adapters import ChannelDescriptor, SourceFrame
from drillai.telemetry.connectors import parse_channel_mappings
from drillai.telemetry.protocols.witsml import (
    WitsmlPollBatch,
    WitsmlSoapClient,
    compute_witsml_next_watermark,
)
from drillai.telemetry.service import PointIn

__all__ = [
    "WITSML_MNEMONIC_ALIASES",
    "WitsmlChannelMap",
    "WitsmlPollingAdapter",
    "resolve_witsml_mnemonic",
]

WITSML_MNEMONIC_ALIASES: dict[str, str] = {
    "DEPT": "depth_md",
    "DBTM": "bit_depth",
    "DVER": "depth_tvd",
    "BPOS": "block_pos",
    "ROPA": "rop",
    "ROP": "rop",
    "HKLA": "hookload",
    "HKLD": "hookload",
    "WOBA": "wob",
    "WOB": "wob",
    "RPMA": "rpm",
    "RPM": "rpm",
    "TQA": "torque",
    "TRQ": "torque",
    "SPPA": "spp",
    "SPP": "spp",
    "MFIA": "flow_in",
    "FLOWIN": "flow_in",
    "MFOA": "flow_out",
    "FLOWOUT": "flow_out",
    "MDIA": "mud_weight_in",
    "MDOA": "mud_weight_out",
    "TVA": "pit_volume_total",
    "ECDA": "ecd",
}


@dataclass(frozen=True)
class WitsmlChannelMap:
    source_mnemonic: str
    channel_key: str
    source_unit: str
    dimension: str


def resolve_witsml_mnemonic(mnemonic: str) -> str | None:
    cleaned = mnemonic.strip().upper()
    return WITSML_MNEMONIC_ALIASES.get(cleaned)


class WitsmlPollingAdapter:
    """Read-only WITSML 1.4.1.1 SOAP polling adapter conforming to ``SourceAdapter``."""

    key = "witsml.1.4.1.1.soap_http"
    source = "witsml"
    source_kind = "witsml"

    def __init__(
        self,
        *,
        endpoint_url: str,
        channel_mappings: list[dict[str, Any]] | None = None,
        config: dict[str, Any] | None = None,
        secrets: dict[str, str] | None = None,
        cursor: dict[str, Any] | None = None,
        well_id: str = "",
        wellbore_id: str | None = None,
        operation_id: str | None = None,
    ) -> None:
        self._endpoint_url = endpoint_url
        self._config = dict(config or {})
        if channel_mappings is not None:
            self._config["channel_mappings"] = list(channel_mappings)
        raw_mappings = self._config.get("channel_mappings") or []
        self._mappings = parse_channel_mappings(raw_mappings)
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
        self._client = WitsmlSoapClient(
            endpoint_url=endpoint_url,
            secrets=self._secrets,
            timeout_seconds=float(self._config.get("timeout_seconds", 10.0)),
            max_retries=int(self._config.get("max_retries", 2)),
            tls_verify=bool(self._config.get("tls_verify", True)),
        )
        self._connected = False
        self._subscribed: set[str] = set()
        self._last_batch: WitsmlPollBatch | None = None
        self._last_consumed_batch: WitsmlPollBatch | None = None

    @property
    def cursor(self) -> dict[str, Any]:
        return dict(self._cursor)

    @property
    def last_batch(self) -> WitsmlPollBatch | None:
        return self._last_batch or self._last_consumed_batch

    async def connect(self) -> None:
        if self._connected and self._last_batch is not None:
            return
        max_pts = int(self._config.get("max_points_per_poll", 250))
        self._last_batch = await self._client.poll_log_batch(
            well_id=self._well_id or "well-probe",
            wellbore_id=self._wellbore_id,
            operation_id=self._operation_id,
            config=self._config,
            watermark=self._cursor,
            max_pages=int(self._config.get("max_pages_per_poll", 3)),
            max_frames=max_pts,
            subscribed_channels=self._subscribed or None,
        )
        self._connected = True

    async def describe_channels(self) -> Sequence[ChannelDescriptor]:
        if not self._connected:
            await self.connect()
        if not self._last_batch:
            return list(self._channels.values())

        discovered_units = {c.mnemonic.upper(): c.unit for c in self._last_batch.discovered_curves if c.unit}
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
                "cannot subscribe to channels the WITSML adapter does not describe",
                details={"unknown": sorted(unknown)},
            )
        self._subscribed = set(channel_keys)

    async def poll(self) -> Sequence[SourceFrame]:
        if not self._connected:
            await self.connect()
        max_pts = int(self._config.get("max_points_per_poll", 250))
        if self._last_batch is not None:
            batch = self._last_batch
            self._last_consumed_batch = batch
            self._last_batch = None
        else:
            batch = await self._client.poll_log_batch(
                well_id=self._well_id or "well-probe",
                wellbore_id=self._wellbore_id,
                operation_id=self._operation_id,
                config=self._config,
                watermark=self._cursor,
                max_pages=int(self._config.get("max_pages_per_poll", 3)),
                max_frames=max_pts,
                subscribed_channels=self._subscribed or None,
            )
            self._last_consumed_batch = batch

        frames = list(batch.frames)
        if self._subscribed:
            filtered: list[SourceFrame] = []
            for f in frames:
                if f.channel_key in self._subscribed:
                    filtered.append(f)
                else:
                    batch.disposition.mapped_frames_emitted = max(
                        0, batch.disposition.mapped_frames_emitted - 1
                    )
                    batch.disposition.unsubscribed_skipped += 1
            frames = filtered

        if len(frames) > max_pts:
            deferred_count = len(frames) - max_pts
            frames = frames[:max_pts]
            batch.disposition.mapped_frames_emitted = len(frames)
            batch.disposition.deferred_over_cap += deferred_count
            batch.disposition.truncated_by_limit = True
            batch.disposition.partial_more_available = True

        has_more = batch.partial_more_available or batch.disposition.truncated_by_limit
        self._cursor = compute_witsml_next_watermark(
            self._cursor,
            frames,
            has_more=has_more,
        )
        return frames

    def normalize(self, frame: SourceFrame) -> PointIn:
        frame.validate()
        descriptor = self._channels.get(frame.channel_key)
        if descriptor is None:
            raise ValidationFailed(
                f"WITSML adapter produced a frame for an undescribed channel {frame.channel_key!r}",
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
