"""CP12 Phase A (P0 Data Integrity) & D3/D4 regression and property tests:
- A2: WITSML batch cap (`max_points_per_poll`), multi-curve row truncation, multi-page resume,
      equal timestamps, worker restarts, and randomized schedule invariant verification.
- A3: ETP per-channel index watermarks (`channel_cursors`), cross-channel non-suppression,
      late out-of-order vs duplicate vs conflicting revision semantics, and over-cap deferral.
- A4: Strict ETP/WITSML timestamp & index conversion (`s`, `ms`, `us`, `ns`, ISO-8601 UTC),
      rejection of `bool`, `NaN`, `Inf`, naive timestamps, out-of-range epochs, and depth-only
      primary indexes, plus secondary depth index preservation.
- A5: Exact source-record disposition accounting (`ProtocolPollDisposition.reconciled`) and
      persistence in `ConnectorRun.details["manifest"]`.
- D3 & D4: Full-payload XML DTD/entity/depth enforcement beyond 4 KB and fail-closed `parse_channel_mappings`.
"""

from __future__ import annotations

import datetime as dt
import html

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from drillai.api.app import create_app
from drillai.core.config import get_settings, reset_settings_cache
from drillai.core.errors import ValidationFailed
from drillai.db.base import Base
from drillai.db.models import (
    Organization,
    Project,
    TimeSeries,
    TimeSeriesPoint,
    Well,
    Wellbore,
)
from drillai.telemetry.connectors import ConnectorTransportError, parse_channel_mappings
from drillai.telemetry.protocols.etp import (
    EtpChannelInfo,
    EtpIndexMetadata,
    EtpSamplePoint,
    EtpWebSocketClient,
    LocalEtpWebSocketServer,
    _parse_etp_index_timestamp,
)
from drillai.telemetry.protocols.witsml import (
    LocalWitsmlSoapServer,
    WitsmlSoapClient,
    parse_get_from_store_response,
)
from drillai.telemetry.worker import ConnectorWorker


def _three_channel_mappings() -> list[dict[str, object]]:
    return [
        {
            "source_mnemonic": "SPP",
            "channel_uri": "eml://witsml20/Channel(spp)",
            "channel_key": "spp",
            "name": "Standpipe Pressure",
            "dimension": "pressure",
            "unit": "psi",
        },
        {
            "source_mnemonic": "WOB",
            "channel_uri": "eml://witsml20/Channel(wob)",
            "channel_key": "wob",
            "name": "Weight on Bit",
            "dimension": "force",
            "unit": "klbf",
        },
        {
            "source_mnemonic": "RPM",
            "channel_uri": "eml://witsml20/Channel(rpm)",
            "channel_key": "rpm",
            "name": "Rotary Speed",
            "dimension": "rotary_speed",
            "unit": "rpm",
        },
    ]


async def _seed_org_and_well(app, org_id: str, well_id: str, wellbore_id: str, slug: str) -> None:
    async with app.state.database.session() as session:
        session.add(Organization(id=org_id, slug=slug, name="CP12 Integrity Org", kind="operator"))
        session.add(Project(id=f"prj_{well_id}", org_id=org_id, name="CP12 Integrity Project"))
        session.add(
            Well(
                id=well_id,
                org_id=org_id,
                project_id=f"prj_{well_id}",
                name=f"WELL-{well_id}",
                well_type="development_producer",
                status="drilling",
            )
        )
        session.add(
            Wellbore(
                id=wellbore_id,
                org_id=org_id,
                well_id=well_id,
                name=f"WB-{wellbore_id}",
                purpose="main",
                status="active",
                is_active=True,
            )
        )
        await session.commit()


# ---------------------------------------------------------------------------
# Phase A2: WITSML Batch Cap, Mid-Row Truncation, Multi-Page & Restart Safety
# ---------------------------------------------------------------------------


async def test_a2_witsml_over_cap_mid_row_and_multipage_zero_data_loss(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    """Verify that when WITSML returns more frames than `max_points_per_poll` (cutting mid-row
    and across multi-page Result=2 boundaries), the watermark advances ONLY through emitted frames
    and subsequent polls (including across worker restarts) ingest 100% of points with zero loss."""
    monkeypatch.setenv("DRILLAI_ENVIRONMENT", "test")
    monkeypatch.setenv("DRILLAI_AUTH_ENABLED", "false")
    monkeypatch.setenv("DRILLAI_BLOB_BACKEND", "memory")
    monkeypatch.setenv("DRILLAI_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path}/a2_witsml.db")
    monkeypatch.setenv("DRILLAI_SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("DRILLAI_CONNECTOR_ALLOW_LOOPBACK", "true")
    reset_settings_cache()
    settings = get_settings()
    app = create_app(settings)

    async with app.state.database.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    org_id = "org_000000000000000000000001"
    well_id = "wel_a2_witsml"
    wellbore_id = "wbr_a2_witsml"
    await _seed_org_and_well(app, org_id, well_id, wellbore_id, settings.dev_org_slug)

    start_dt = dt.datetime(2026, 5, 1, 10, 0, 0, tzinfo=dt.UTC)
    server = LocalWitsmlSoapServer(page_size_override=2)
    server.seed_default_rows(start=start_dt, count=5, step_seconds=5.0)
    # 5 rows * 3 mapped curves (SPP, WOB, RPM) = 15 expected points.
    # Set max_points_per_poll = 4 so Poll 1 cuts mid-row on Row 2 (after SPP at T+5s, deferring WOB and RPM at T+5s).

    try:
        async with server:
            worker1 = ConnectorWorker(
                app.state.database,
                worker_id="wrk_a2_1",
                lease_seconds=1.0,
                allow_loopback=True,
            )
            app.state.connector_worker = worker1

            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                hdrs = {"X-Dev-Roles": "engineer"}
                create_res = await client.post(
                    "/api/v1/connectors",
                    headers=hdrs,
                    json={
                        "key": "witsml-a2-cap",
                        "name": "WITSML A2 Cap Store",
                        "protocol_profile": "witsml.1.4.1.1.soap_http",
                        "well_id": well_id,
                        "wellbore_id": wellbore_id,
                        "endpoint_url": server.endpoint_url,
                        "config": {
                            "auth_mode": "none",
                            "poll_interval_seconds": 0.05,
                            "max_points_per_poll": 4,
                            "max_pages_per_poll": 3,
                            "channel_mappings": _three_channel_mappings(),
                        },
                    },
                )
                assert create_res.status_code == 201, create_res.text
                cid = create_res.json()["id"]

                await client.post(
                    f"/api/v1/connectors/{cid}/start",
                    headers=hdrs,
                    json={"reason": "start A2 test"},
                )

                # Poll 1: emits 4 points (Row 0: SPP, WOB, RPM; Row 1: SPP).
                # Row 1's WOB and RPM must be deferred, and cursor_timestamp must stop at Row 1 (T+5s)
                # with completed_at_cursor = ["witsml:spp:2026-05-01T10:00:05+00:00"]
                p1 = (await client.post(f"/api/v1/connectors/{cid}/poll", headers=hdrs)).json()
                out1 = p1["outcome"]
                assert out1["status"] == "succeeded"
                assert out1["frames_received"] == 4
                assert out1["points_accepted"] == 4
                assert out1["checkpoint_status"] == "partial"
                assert out1["manifest"]["reconciled"] is True
                assert out1["manifest"]["deferred_over_cap"] >= 2
                row1_iso = (start_dt + dt.timedelta(seconds=5)).isoformat()
                assert out1["cursor_after"]["cursor_timestamp"] == row1_iso
                assert out1["cursor_after"]["completed_at_cursor"] == [f"witsml:spp:{row1_iso}"]

                # Poll 2: resumes at T+5s, suppresses already-ingested SPP at T+5s, and emits
                # WOB & RPM at T+5s plus SPP & WOB at T+10s (4 points).
                p2 = (await client.post(f"/api/v1/connectors/{cid}/poll", headers=hdrs)).json()
                out2 = p2["outcome"]
                assert out2["status"] == "succeeded"
                assert out2["frames_received"] == 4
                assert out2["points_accepted"] == 4
                assert out2["manifest"]["watermark_suppressed"] >= 1
                assert out2["manifest"]["reconciled"] is True

                # Simulate worker restart: expire worker1's lease and switch to worker2
                worker2 = ConnectorWorker(
                    app.state.database,
                    worker_id="wrk_a2_2",
                    lease_seconds=15.0,
                    allow_loopback=True,
                )
                app.state.connector_worker = worker2

                # Poll 3 & Poll 4 with worker2 to drain remaining 7 points (4 + 3 = 7)
                p3 = (await client.post(f"/api/v1/connectors/{cid}/poll", headers=hdrs)).json()
                assert p3["outcome"]["points_accepted"] == 4
                assert p3["outcome"]["manifest"]["reconciled"] is True

                p4 = (await client.post(f"/api/v1/connectors/{cid}/poll", headers=hdrs)).json()
                assert p4["outcome"]["points_accepted"] == 3
                assert p4["outcome"]["checkpoint_status"] == "complete"
                assert p4["outcome"]["manifest"]["reconciled"] is True

                # Poll 5: all 15 points already ingested; 0 new frames
                p5 = (await client.post(f"/api/v1/connectors/{cid}/poll", headers=hdrs)).json()
                assert p5["outcome"]["frames_received"] == 0
                assert p5["outcome"]["points_accepted"] == 0
                assert p5["outcome"]["checkpoint_status"] == "unchanged"

            # Verify exact DB point identities across all 3 channels (5 timestamps * 3 channels = 15 points)
            async with app.state.database.session() as session:
                series_rows = (
                    (await session.execute(select(TimeSeries).where(TimeSeries.well_id == well_id)))
                    .scalars()
                    .all()
                )
                by_key = {s.channel_key: s.id for s in series_rows}
                assert set(by_key.keys()) == {"spp", "wob", "rpm"}

                all_pts = (
                    (
                        await session.execute(
                            select(TimeSeriesPoint).where(
                                TimeSeriesPoint.series_id.in_(list(by_key.values()))
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                assert len(all_pts) == 15
                expected_ids = {
                    f"witsml:{ch}:{(start_dt + dt.timedelta(seconds=idx * 5)).isoformat()}"
                    for idx in range(5)
                    for ch in ("spp", "wob", "rpm")
                }
                actual_ids = {pt.source_point_id for pt in all_pts}
                assert actual_ids == expected_ids
    finally:
        await app.state.database.dispose()


@pytest.mark.parametrize("cap_size,page_size", [(1, 1), (2, 2), (3, 2), (5, 3), (7, 4), (50, 10)])
async def test_a2_witsml_randomized_cap_and_page_schedule_invariant(cap_size: int, page_size: int):
    """Property test verifying that for any combination of `max_frames` cap and `maxReturnNodes`
    page size, iterative polling yields the exact ordered set of valid source points without
    ever advancing `cursor_timestamp` ahead of unemitted rows."""
    start_dt = dt.datetime(2026, 5, 2, 0, 0, 0, tzinfo=dt.UTC)
    server = LocalWitsmlSoapServer(page_size_override=page_size)
    server.seed_default_rows(start=start_dt, count=6, step_seconds=10.0)
    # Add an equal-timestamp row with an updated/extra curve to test equal-timestamp boundary safety
    mappings = parse_channel_mappings(_three_channel_mappings())

    async with server:
        client = WitsmlSoapClient(endpoint_url=server.endpoint_url, timeout_seconds=5.0)
        watermark: dict[str, object] = {}
        collected_ids: list[str] = []

        for _ in range(30):
            batch = await client.poll_log_batch(
                well_id="wel_prop",
                config={
                    "channel_mappings": _three_channel_mappings(),
                    "max_points_per_poll": cap_size,
                },
                watermark=watermark,
                max_pages=2,
                max_frames=cap_size,
            )
            assert batch.disposition.reconciled is True
            assert len(batch.frames) <= cap_size
            if not batch.frames:
                break
            max_emitted_ts = max(f.ts for f in batch.frames).isoformat()
            assert batch.next_watermark["cursor_timestamp"] == max_emitted_ts
            for f in batch.frames:
                assert f.source_point_id is not None
                collected_ids.append(f.source_point_id)
            watermark = batch.next_watermark

        expected_ids = {
            f"witsml:{m.channel_key}:{(start_dt + dt.timedelta(seconds=idx * 10)).isoformat()}"
            for idx in range(6)
            for m in mappings
        }
        assert len(collected_ids) == len(expected_ids) == 18
        assert set(collected_ids) == expected_ids


# ---------------------------------------------------------------------------
# Phase A3: ETP Per-Channel Index Watermarks & Out-of-Order vs Duplicate/Conflict
# ---------------------------------------------------------------------------


async def test_a3_etp_per_channel_cursors_never_suppress_slower_channels_and_classify_revisions(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    """Verify:
    1. When Channel 1 (SPP) advances to T3, Channel 2 (WOB) points at T1.5 and T2.5 arriving
       in a later poll are NOT suppressed by SPP's T3 watermark.
    2. Reconnect/replay of already-ingested points with identical values is suppressed (`watermark_suppressed`).
    3. A point on the same channel with an earlier timestamp than the channel's max timestamp
       that was NOT previously ingested is accepted and marked `is_out_of_order=True`.
    4. A replayed point with the same `source_point_id` but a conflicting value is flagged as
       a conflict/rejection by `TelemetryService`.
    5. Over-cap `ChannelData` defers remaining items without advancing channel watermarks past them.
    """
    monkeypatch.setenv("DRILLAI_ENVIRONMENT", "test")
    monkeypatch.setenv("DRILLAI_AUTH_ENABLED", "false")
    monkeypatch.setenv("DRILLAI_BLOB_BACKEND", "memory")
    monkeypatch.setenv("DRILLAI_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path}/a3_etp.db")
    monkeypatch.setenv("DRILLAI_SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("DRILLAI_CONNECTOR_ALLOW_LOOPBACK", "true")
    reset_settings_cache()
    settings = get_settings()
    app = create_app(settings)

    async with app.state.database.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    org_id = "org_000000000000000000000001"
    well_id = "wel_a3_etp"
    wellbore_id = "wbr_a3_etp"
    await _seed_org_and_well(app, org_id, well_id, wellbore_id, settings.dev_org_slug)

    t1 = dt.datetime(2026, 5, 3, 12, 0, 0, tzinfo=dt.UTC)
    t1_5 = t1 + dt.timedelta(seconds=5)
    t2 = t1 + dt.timedelta(seconds=10)
    t2_2 = t1 + dt.timedelta(seconds=12)
    t2_5 = t1 + dt.timedelta(seconds=15)
    t3 = t1 + dt.timedelta(seconds=20)

    # Poll 1 data: SPP has T1, T2, T3; WOB only has T1
    server = LocalEtpWebSocketServer()
    server.points = [
        EtpSamplePoint(1, "SPP", t1.isoformat(), 3000.0, 2500.0),
        EtpSamplePoint(1, "SPP", t2.isoformat(), 3020.0, 2500.5),
        EtpSamplePoint(1, "SPP", t3.isoformat(), 3040.0, 2501.0),
        EtpSamplePoint(2, "WOB", t1.isoformat(), 20.0, 2500.0),
    ]

    try:
        async with server:
            worker = ConnectorWorker(
                app.state.database,
                worker_id="wrk_a3_etp",
                lease_seconds=15.0,
                allow_loopback=True,
            )
            app.state.connector_worker = worker

            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                hdrs = {"X-Dev-Roles": "engineer"}
                c_res = await client.post(
                    "/api/v1/connectors",
                    headers=hdrs,
                    json={
                        "key": "etp-a3-cursors",
                        "name": "ETP A3 Per-Channel Stream",
                        "protocol_profile": "etp.1.2.json_ws",
                        "well_id": well_id,
                        "wellbore_id": wellbore_id,
                        "endpoint_url": server.endpoint_url,
                        "config": {
                            "auth_mode": "none",
                            "poll_interval_seconds": 0.05,
                            "max_points_per_poll": 10,
                            "channel_mappings": _three_channel_mappings(),
                        },
                    },
                )
                assert c_res.status_code == 201, c_res.text
                cid = c_res.json()["id"]

                await client.post(
                    f"/api/v1/connectors/{cid}/start",
                    headers=hdrs,
                    json={"reason": "start ETP A3"},
                )

                # Poll 1: ingests 4 points (SPP: T1, T2, T3; WOB: T1)
                p1 = (await client.post(f"/api/v1/connectors/{cid}/poll", headers=hdrs)).json()
                out1 = p1["outcome"]
                assert out1["status"] == "succeeded"
                assert out1["points_accepted"] == 4
                ch_cursors1 = out1["cursor_after"]["channel_cursors"]
                assert ch_cursors1["spp"]["last_timestamp"] == t3.isoformat()
                assert ch_cursors1["wob"]["last_timestamp"] == t1.isoformat()

                # Poll 2: WOB emits T1.5 and T2.5 (both < SPP's T3, but > WOB's T1!).
                # Both WOB points MUST be accepted!
                server.points.extend(
                    [
                        EtpSamplePoint(2, "WOB", t1_5.isoformat(), 21.5, 2500.2),
                        EtpSamplePoint(2, "WOB", t2_5.isoformat(), 22.5, 2500.8),
                    ]
                )
                p2 = (await client.post(f"/api/v1/connectors/{cid}/poll", headers=hdrs)).json()
                out2 = p2["outcome"]
                assert out2["status"] == "succeeded"
                assert out2["points_accepted"] == 2
                ch_cursors2 = out2["cursor_after"]["channel_cursors"]
                assert ch_cursors2["spp"]["last_timestamp"] == t3.isoformat()
                assert ch_cursors2["wob"]["last_timestamp"] == t2_5.isoformat()

                # Poll 3: Force server to replay all items AND include:
                # - a late out-of-order point on SPP at T2.2 (< T3, never seen before) -> accepted & out_of_order=1
                # - a conflicting revision on SPP at T3 (value changed from 3040.0 to 9999.0) -> conflict/rejected=1
                server.fault_mode = "replay_duplicates"
                server.points = [
                    EtpSamplePoint(1, "SPP", t1.isoformat(), 3000.0, 2500.0),  # duplicate -> suppressed
                    EtpSamplePoint(
                        1, "SPP", t2_2.isoformat(), 3025.0, 2500.6
                    ),  # late out-of-order -> accepted
                    EtpSamplePoint(
                        1, "SPP", t3.isoformat(), 9999.0, 2501.0
                    ),  # conflict -> rejected by service
                ]
                p3 = (await client.post(f"/api/v1/connectors/{cid}/poll", headers=hdrs)).json()
                out3 = p3["outcome"]
                assert out3["status"] == "succeeded"
                assert out3["points_accepted"] == 1  # T2.2 accepted
                assert out3["points_rejected"] >= 1  # T3 conflicting value rejected
                assert out3["manifest"]["out_of_order"] >= 1
                assert out3["manifest"]["conflicts"] >= 1
                assert out3["manifest"]["watermark_suppressed"] >= 1
                assert out3["manifest"]["reconciled"] is True
                # SPP watermark must not regress from T3
                assert out3["cursor_after"]["channel_cursors"]["spp"]["last_timestamp"] == t3.isoformat()
    finally:
        await app.state.database.dispose()


# ---------------------------------------------------------------------------
# Phase A4: Strict Timestamp & Index Conversion & Rejection of Invalid Encodings
# ---------------------------------------------------------------------------


def test_a4_etp_index_timestamp_conversion_units_and_rejections():
    """Verify explicit index unit conversions (`s`, `ms`, `us`, `ns`, ISO-8601 UTC) and
    rejection of `bool`, `NaN`, `Inf`, naive strings, out-of-range epochs, and depth primary indexes."""
    expected_dt = dt.datetime(2026, 4, 10, 12, 0, 0, tzinfo=dt.UTC)
    epoch_s = int(expected_dt.timestamp())

    # 1. Explicit units convert to exact UTC datetime without magnitude guessing
    dt_s, err_s = _parse_etp_index_timestamp(epoch_s, index_kind="DateTime", index_uom="s")
    assert err_s is None and dt_s == expected_dt

    dt_ms, err_ms = _parse_etp_index_timestamp(epoch_s * 1_000, index_kind="DateTime", index_uom="ms")
    assert err_ms is None and dt_ms == expected_dt

    dt_us, err_us = _parse_etp_index_timestamp(epoch_s * 1_000_000, index_kind="DateTime", index_uom="us")
    assert err_us is None and dt_us == expected_dt

    dt_ns, err_ns = _parse_etp_index_timestamp(epoch_s * 1_000_000_000, index_kind="DateTime", index_uom="ns")
    assert err_ns is None and dt_ns == expected_dt

    dt_iso, err_iso = _parse_etp_index_timestamp(
        "2026-04-10T12:00:00Z", index_kind="DateTime", index_uom="us"
    )
    assert err_iso is None and dt_iso == expected_dt

    # 2. Milliseconds sent on a microsecond channel (1.77e12 us -> 1970-01-21) is rejected as out-of-range
    dt_wrong_unit, err_wrong_unit = _parse_etp_index_timestamp(
        epoch_s * 1_000, index_kind="DateTime", index_uom="us"
    )
    assert dt_wrong_unit is None
    assert err_wrong_unit == "timestamp_out_of_range"

    # 3. Booleans are rejected (even though isinstance(True, int) is True in Python)
    for bval in (True, False):
        dt_b, err_b = _parse_etp_index_timestamp(bval, index_kind="DateTime", index_uom="s")
        assert dt_b is None
        assert err_b == "boolean_timestamp_rejected"

    # 4. NaN and +/-Inf are rejected
    for non_finite in (float("nan"), float("inf"), float("-inf")):
        dt_nf, err_nf = _parse_etp_index_timestamp(non_finite, index_kind="DateTime", index_uom="s")
        assert dt_nf is None
        assert err_nf == "non_finite_timestamp"

    # 5. Naive ISO timestamp without timezone is rejected
    dt_naive, err_naive = _parse_etp_index_timestamp(
        "2026-04-10T12:00:00", index_kind="DateTime", index_uom="us"
    )
    assert dt_naive is None
    assert err_naive == "malformed_timestamp"

    # 6. Depth-indexed primary channel is rejected as unsupported_primary_index_kind
    dt_depth, err_depth = _parse_etp_index_timestamp(2500.0, index_kind="MeasuredDepth", index_uom="m")
    assert dt_depth is None
    assert err_depth == "unsupported_primary_index_kind"


async def test_a4_a5_etp_mixed_batch_disposition_and_secondary_depth_conversion():
    """Verify that an ETP stream with declared numeric index UOMs, secondary depth indexes in feet,
    unmapped channels, unknown channelIds, malformed timestamps, booleans, NaNs, and depth-only
    channels accounts for every single item in `batch.disposition` (`reconciled == True`)."""
    t0 = dt.datetime(2026, 5, 4, 8, 0, 0, tzinfo=dt.UTC)
    epoch_ms = int(t0.timestamp() * 1000)

    server = LocalEtpWebSocketServer(
        channels=[
            # Channel 1: primary DateTime in ms, secondary MeasuredDepth in ft
            EtpChannelInfo(
                channel_id=1,
                mnemonic="SPP",
                unit="psi",
                channel_name="SPP",
                uri="eml://witsml20/Channel(spp)",
                indexes=(
                    EtpIndexMetadata(index_kind="DateTime", uom="ms"),
                    EtpIndexMetadata(index_kind="MeasuredDepth", uom="ft"),
                ),
            ),
            # Channel 2: depth-only primary index -> must be rejected as unsupported_primary_index_kind
            EtpChannelInfo(
                channel_id=2,
                mnemonic="WOB",
                unit="klbf",
                channel_name="WOB",
                uri="eml://witsml20/Channel(wob)",
                indexes=(EtpIndexMetadata(index_kind="MeasuredDepth", uom="m"),),
            ),
            # Channel 99: unmapped channel -> must be counted as unmapped_skipped
            EtpChannelInfo(
                channel_id=99,
                mnemonic="GAMMA",
                unit="gAPI",
                channel_name="GAMMA",
                uri="eml://witsml20/Channel(gamma)",
                indexes=(EtpIndexMetadata(index_kind="DateTime", uom="ms"),),
            ),
        ],
        data_items=[
            # 1. Valid SPP point with ms epoch and secondary depth 1000 ft (304.8 m)
            {
                "channelId": 1,
                "indexes": [{"item": epoch_ms}, {"item": 1000.0}],
                "value": {"item": 3100.0},
            },
            # 2. Exact in-batch duplicate of item 1
            {
                "channelId": 1,
                "indexes": [{"item": epoch_ms}, {"item": 1000.0}],
                "value": {"item": 3100.0},
            },
            # 3. Boolean timestamp on SPP -> rejected_malformed_timestamp
            {
                "channelId": 1,
                "indexes": [{"item": True}],
                "value": {"item": 3110.0},
            },
            # 4. Non-finite value (NaN) on SPP -> rejected_non_finite_value
            {
                "channelId": 1,
                "indexes": [{"item": epoch_ms + 5000}],
                "value": {"item": "NaN"},
            },
            # 5. Boolean value on SPP -> rejected_invalid_value
            {
                "channelId": 1,
                "indexes": [{"item": epoch_ms + 10000}],
                "value": {"item": False},
            },
            # 6. Depth-indexed primary channel (Channel 2 WOB) -> rejected_unsupported_index
            {
                "channelId": 2,
                "indexes": [{"item": 2450.0}],
                "value": {"item": 25.0},
            },
            # 7. Unmapped channel (Channel 99 GAMMA) -> unmapped_skipped
            {
                "channelId": 99,
                "indexes": [{"item": epoch_ms}],
                "value": {"item": 85.0},
            },
            # 8. Unknown channelId (Channel 777 not in ChannelMetadata) -> rejected_unknown_channel
            {
                "channelId": 777,
                "indexes": [{"item": epoch_ms}],
                "value": {"item": 12.0},
            },
        ],
    )

    async with server:
        client = EtpWebSocketClient(endpoint_url=server.endpoint_url, timeout_seconds=5.0)
        batch = await client.poll_subscription_batch(
            well_id="wel_a4_a5",
            config={"channel_mappings": _three_channel_mappings()},
            watermark={},
        )
        disp = batch.disposition
        assert disp.reconciled is True
        assert disp.source_records_discovered == 8
        assert disp.mapped_frames_emitted == 1
        assert disp.in_batch_duplicates == 1
        assert disp.unmapped_skipped == 1
        assert disp.rejected_malformed_timestamp == 1
        assert disp.rejected_non_finite_value == 1
        assert disp.rejected_invalid_value == 1
        assert disp.rejected_unsupported_index == 1
        assert disp.rejected_unknown_channel == 1
        assert disp.total_protocol_rejected == 5

        assert len(batch.frames) == 1
        frame = batch.frames[0]
        assert frame.channel_key == "spp"
        assert frame.ts == t0
        assert frame.value == 3100.0
        assert frame.depth_md_si == pytest.approx(304.8, rel=1e-6)


# ---------------------------------------------------------------------------
# Phase D3 & D4: Full-Payload XML Safety & Fail-Closed Channel Mappings
# ---------------------------------------------------------------------------


def test_d3_witsml_blocks_doctype_entity_after_4kb_and_deep_xml():
    """Verify that DOCTYPE/ENTITY declarations placed after >4096 bytes of padding,
    custom entity references inside XMLout, and deeply nested XML (>32 levels) are blocked."""
    padding = "<!-- " + ("X" * 6000) + " -->"
    late_doctype_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f"{padding}"
        '<!DOCTYPE logs [<!ENTITY xxe SYSTEM "file:///etc/hostname">]>'
        '<logs xmlns="http://www.witsml.org/schemas/1series" version="1.4.1.1"></logs>'
    )
    with pytest.raises(ConnectorTransportError) as exc_late:
        parse_get_from_store_response(late_doctype_xml, mappings=[], well_id="wel_sec")
    assert exc_late.value.category == "malformed_payload"
    assert exc_late.value.retryable is False

    # Nested XMLout inside SOAP response containing DOCTYPE after 5000 chars
    inner_bad = f"{padding}<!DOCTYPE foo><logs/>"
    soap_wrapped = (
        '<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/">'
        "<soapenv:Body><WMLS_GetFromStoreResponse><Result>1</Result>"
        f"<XMLout>{html.escape(inner_bad)}</XMLout>"
        "</WMLS_GetFromStoreResponse></soapenv:Body></soapenv:Envelope>"
    )
    with pytest.raises(ConnectorTransportError) as exc_inner:
        parse_get_from_store_response(soap_wrapped, mappings=[], well_id="wel_sec")
    assert exc_inner.value.category == "malformed_payload"

    # Excessive nesting depth (>32)
    open_tags = "".join(f"<n{i}>" for i in range(40))
    close_tags = "".join(f"</n{i}>" for i in reversed(range(40)))
    deep_xml = f"<logs>{open_tags}{close_tags}</logs>"
    with pytest.raises(ConnectorTransportError) as exc_deep:
        parse_get_from_store_response(deep_xml, mappings=[], well_id="wel_sec")
    assert exc_deep.value.category == "malformed_payload"


def test_d4_parse_channel_mappings_fails_closed_on_invalid_entries():
    """Verify `parse_channel_mappings` never silently drops malformed, incomplete, or duplicate entries."""
    with pytest.raises(ValidationFailed):
        parse_channel_mappings("not-a-list")

    with pytest.raises(ValidationFailed):
        parse_channel_mappings(["not-a-dict"])

    with pytest.raises(ValidationFailed):
        parse_channel_mappings([{"source_mnemonic": "SPP", "channel_key": "spp"}])  # missing unit/dimension

    with pytest.raises(ValidationFailed):
        parse_channel_mappings(
            [
                {
                    "source_mnemonic": "SPP",
                    "channel_key": "spp",
                    "dimension": "invalid_dimension",
                    "unit": "psi",
                }
            ]
        )

    with pytest.raises(ValidationFailed):
        parse_channel_mappings(
            [
                {
                    "source_mnemonic": "SPP",
                    "channel_key": "spp",
                    "dimension": "pressure",
                    "unit": "not_a_unit",
                }
            ]
        )

    # Duplicate source_mnemonic fails closed
    with pytest.raises(ValidationFailed):
        parse_channel_mappings(
            [
                {
                    "source_mnemonic": "SPP",
                    "channel_key": "spp",
                    "dimension": "pressure",
                    "unit": "psi",
                },
                {
                    "source_mnemonic": "spp",
                    "channel_key": "spp_aux",
                    "dimension": "pressure",
                    "unit": "psi",
                },
            ]
        )
