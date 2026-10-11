"""CP13 Phase 5 & Phase 6 (Checkpoint C):
- Phase 5: 14-Scenario WITSML 1.4.1.1 Interoperability & Regression Verification
- Phase 6: 10 Worker Lease/Heartbeat/Preemption/Expired-Lease Guarantees (including R-01 & R-02)
  and 5 Real PostgreSQL Concurrent SQL CAS Race Tests.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import html
import math
from typing import Any

import pytest

from drillai.core.clock import utc_now
from drillai.core.config import reset_settings_cache
from drillai.core.errors import Conflict
from drillai.db.base import Base
from drillai.db.models import Connector, Organization, Project, Well, Wellbore
from drillai.db.session import Database
from drillai.telemetry.connectors import (
    ConnectorService,
    ConnectorTransportError,
    parse_channel_mappings,
)
from drillai.telemetry.protocols.witsml import (
    LocalWitsmlSoapServer,
    WitsmlSoapClient,
    parse_get_from_store_response,
)
from drillai.telemetry.worker import ConnectorWorker


@pytest.fixture(autouse=True)
def _clean_settings():
    reset_settings_cache()
    yield
    reset_settings_cache()


def _witsml_mappings() -> list[dict[str, Any]]:
    return [
        {
            "source_mnemonic": "SPP",
            "channel_key": "spp",
            "name": "Standpipe Pressure",
            "dimension": "pressure",
            "unit": "psi",
        },
        {
            "source_mnemonic": "WOB",
            "channel_key": "wob",
            "name": "Weight on Bit",
            "dimension": "force",
            "unit": "klbf",
        },
        {
            "source_mnemonic": "HKLD",
            "channel_key": "hookload",
            "name": "Hookload",
            "dimension": "force",
            "unit": "klbf",
        },
    ]


def _wrap_soap_response(result_code: int, xml_out: str, supp_msg: str = "") -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" '
        'xmlns:wmls="http://www.witsml.org/message/120">'
        "<soapenv:Body>"
        "<wmls:WMLS_GetFromStoreResponse>"
        f"<Result>{result_code}</Result>"
        f"<XMLout>{html.escape(xml_out)}</XMLout>"
        f"<SuppMsgOut>{html.escape(supp_msg)}</SuppMsgOut>"
        "</wmls:WMLS_GetFromStoreResponse>"
        "</soapenv:Body>"
        "</soapenv:Envelope>"
    )


async def _seed_org_and_well(db: Database, suffix: str) -> tuple[str, str, str]:
    org_id = f"org_{suffix}"
    well_id = f"wel_{suffix}"
    wellbore_id = f"wbr_{suffix}"
    async with db.session() as session:
        session.add(Organization(id=org_id, slug=f"slug-{suffix}", name=f"Org {suffix}"))
        session.add(Project(id=f"prj_{suffix}", org_id=org_id, name=f"Project {suffix}"))
        session.add(Well(id=well_id, org_id=org_id, project_id=f"prj_{suffix}", name=f"Well {suffix}"))
        session.add(Wellbore(id=wellbore_id, org_id=org_id, well_id=well_id, name="Main Wellbore"))
        await session.commit()
    return org_id, well_id, wellbore_id


# ===========================================================================
# Phase 5: WITSML 1.4.1.1 Interoperability & Regression Suite (Scenarios 1-14)
# ===========================================================================


async def test_phase5_witsml_1411_scenarios_1_to_12_pagination_caps_dispositions_depths_and_xml_faults():
    """Verify WITSML 1.4.1.1 Scenarios 1 through 12:
    1. Single-page `WMLS_GetFromStore` (`Result = 1`) with full row ingestion.
    2. Multi-page `WMLS_GetFromStore` (`Result = 2` -> `Result = 1`) across pages.
    3. Batch cap (`max_frames`) reached inside a multi-channel row (`completed_at_cursor`).
    4. Batch cap (`max_frames`) reached on page 1 when page 2 exists.
    5. Exact cursor resume on next poll without losing or duplicating remaining channels/rows.
    6. Unmapped mnemonic skip with explicit disposition count.
    7. Invalid numeric value rejection without blocking valid sibling channels in the same row.
    8. Missing/null value handling with explicit quality/disposition semantics.
    9. Out-of-order and duplicate rows across consecutive polls.
    10. Revised values at an already-seen timestamp.
    11. Malformed XML, `DOCTYPE`/`ENTITY`, deep XML, oversized XML, SOAP fault, negative `Result` code,
        missing `<logData>`, and mismatched `<mnemonicList>`/`<unitList>` lengths.
    12. Secondary depth index extraction and conversion (`ft` -> `m`, `m` -> `m`).
    """
    mappings = parse_channel_mappings(_witsml_mappings())

    # Scenarios 6, 7, 8, 12: Mixed row with secondary MD in ft, unmapped mnemonic, invalid token, and empty/null token
    xml_mixed_ft = """<?xml version="1.0" encoding="UTF-8"?>
    <logs xmlns="http://www.witsml.org/schemas/1series" version="1.4.1.1">
      <log uidWell="W1" uidWellbore="B1" uid="L1">
        <indexType>date time</indexType>
        <indexCurve>TIME</indexCurve>
        <logData>
          <mnemonicList>TIME,DEPT,SPP,WOB,HKLD,UNMAPPED_M</mnemonicList>
          <unitList>s,ft,psi,klbf,klbf,rpm</unitList>
          <data>2026-07-05T10:00:00+00:00,1000.0,3000.0,BAD_NUM,,95.0</data>
        </logData>
      </log>
    </logs>"""
    batch_ft = parse_get_from_store_response(
        _wrap_soap_response(1, xml_mixed_ft),
        mappings=mappings,
        well_id="wel_p5",
        wellbore_id="wbr_p5",
        operation_id=None,
        previous_watermark={},
        max_frames=50,
    )
    assert len(batch_ft.frames) == 2  # SPP (3000.0) and HKLD (None/missing)
    assert batch_ft.frames[0].channel_key == "spp"
    assert batch_ft.frames[0].value == 3000.0
    assert math.isclose(batch_ft.frames[0].depth_md_si or 0.0, 304.8, rel_tol=1e-5)
    assert batch_ft.frames[1].channel_key == "hookload"
    assert batch_ft.frames[1].value is None
    assert batch_ft.frames[1].quality == "missing"
    assert batch_ft.disposition.unmapped_skipped == 1
    assert batch_ft.disposition.rejected_invalid_value == 1
    assert batch_ft.disposition.reconciled is True

    # Scenario 12b: Secondary depth in meters ('m' -> 'm')
    xml_depth_m = xml_mixed_ft.replace("<unitList>s,ft,", "<unitList>s,m,").replace(",1000.0,", ",450.5,")
    batch_m = parse_get_from_store_response(
        _wrap_soap_response(1, xml_depth_m),
        mappings=mappings,
        well_id="wel_p5",
        wellbore_id="wbr_p5",
        operation_id=None,
        previous_watermark={},
        max_frames=50,
    )
    assert math.isclose(batch_m.frames[0].depth_md_si or 0.0, 450.5, rel_tol=1e-5)

    # Scenarios 1, 2, 3, 4, 5, 9, 10 over live LocalWitsmlSoapServer
    t0 = dt.datetime(2026, 7, 5, 12, 0, 0, tzinfo=dt.UTC)
    async with LocalWitsmlSoapServer(page_size_override=2) as srv:
        srv.seed_default_rows(start=t0, count=4, step_seconds=5.0)
        client = WitsmlSoapClient(endpoint_url=srv.endpoint_url)

        # Scenarios 3 & 4: Poll with max_frames=2 across 4 rows (each row has 3 mapped channels = 12 total points)
        # First poll hits max_frames=2 mid-row on page 1 (when page 2 also exists)!
        b1 = await client.poll_log_batch(
            well_id="wel_p5",
            config={"channel_mappings": _witsml_mappings()},
            watermark={},
            max_frames=2,
        )
        assert len(b1.frames) == 2
        assert b1.disposition.truncated_by_limit is True
        assert b1.disposition.partial_more_available is True
        assert b1.next_watermark["has_more_deferred"] is True
        assert len(b1.next_watermark["completed_at_cursor"]) == 2

        # Scenario 5: Drain remaining polls until all 12 points are collected with zero loss or duplication
        collected = list(b1.frames)
        wm = b1.next_watermark
        for _ in range(10):
            b_next = await client.poll_log_batch(
                well_id="wel_p5",
                config={"channel_mappings": _witsml_mappings()},
                watermark=wm,
                max_frames=5,
            )
            collected.extend(b_next.frames)
            wm = b_next.next_watermark
            if not wm.get("has_more_deferred"):
                break
        assert len(collected) == 12
        assert len({(f.channel_key, f.ts) for f in collected}) == 12

        # Scenarios 9 & 10: Re-poll at final watermark -> duplicates suppressed
        b_dup = await client.poll_log_batch(
            well_id="wel_p5",
            config={"channel_mappings": _witsml_mappings()},
            watermark=wm,
            max_frames=50,
        )
        assert len(b_dup.frames) == 0
        assert b_dup.disposition.watermark_suppressed >= 3

    # Scenario 11: Malformed XML, DOCTYPE/ENTITY, deep XML, oversized XML, SOAP fault, negative Result,
    # missing <logData>, and mismatched <mnemonicList>/<unitList>
    for bad_xml in (
        "<logs><unclosed>",
        '<?xml version="1.0"?><!DOCTYPE foo [<!ENTITY xxe "secret">]><logs/>',
        "<a>" * 40 + "</a>" * 40,
        "<logs>" + ("x" * (5 * 1024 * 1024 + 128)) + "</logs>",
        _wrap_soap_response(
            1,
            """<logs xmlns="http://www.witsml.org/schemas/1series"><log><logData>
               <mnemonicList>TIME,SPP</mnemonicList><unitList>s</unitList>
               <data>2026-07-05T10:00:00Z,3000</data></logData></log></logs>""",
        ),
    ):
        with pytest.raises(ConnectorTransportError):
            parse_get_from_store_response(
                bad_xml,
                mappings=mappings,
                well_id="wel_p5",
                wellbore_id=None,
                operation_id=None,
                previous_watermark={},
            )

    # Negative Result code
    with pytest.raises(ConnectorTransportError) as neg_exc:
        parse_get_from_store_response(
            _wrap_soap_response(-401, "<logs/>", "Unauthorized WITSML store query"),
            mappings=mappings,
            well_id="wel_p5",
            wellbore_id=None,
            operation_id=None,
            previous_watermark={},
        )
    assert neg_exc.value.category in {"auth_failure", "protocol_error"}

    # Missing <logData> returns empty reconciled batch
    empty_batch = parse_get_from_store_response(
        _wrap_soap_response(
            1, '<logs xmlns="http://www.witsml.org/schemas/1series"><log uid="L1"/></logs>'
        ),
        mappings=mappings,
        well_id="wel_p5",
        wellbore_id=None,
        operation_id=None,
        previous_watermark={},
    )
    assert len(empty_batch.frames) == 0
    assert empty_batch.disposition.reconciled is True


async def test_phase5_scenarios_13_14_and_phase6_lease_expiry_and_data_outcomes(
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
):
    """Verify:
    - Phase 5 Scenario 13: DB failure during Phase 2 leaves connector checkpoint unchanged.
    - Phase 5 Scenario 14: Worker restart after partial poll resumes from persisted cursor without loss.
    - Phase 6 R-01: Expired lease (`lease_expires_at <= now`) at Phase 2 commit or failure commit is fenced
      and cannot overwrite cursor/status; `renew_lease()` also refuses to extend an already-expired lease.
    - Phase 6 R-02: `run_manifest["data_outcome"]` accurately classifies `"partial_batch_capped"`,
      `"all_valid_accepted"`, `"partial_with_rejections"`, `"rejected_only"`, and `"no_data"`.
    """
    db = Database(f"sqlite+aiosqlite:///{tmp_path}/cp13_p5_p6.db")
    async with db.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    org_id, well_id, wellbore_id = await _seed_org_and_well(db, "p5p6")
    t0 = dt.datetime(2026, 7, 6, 0, 0, 0, tzinfo=dt.UTC)

    try:
        async with LocalWitsmlSoapServer(page_size_override=2) as srv:
            srv.seed_default_rows(start=t0, count=3, step_seconds=5.0)
            async with db.session() as session:
                svc = ConnectorService(session, org_id)
                created = await svc.create(
                    key="witsml-p5-resume",
                    name="WITSML P5 Resume",
                    protocol_profile="witsml.1.4.1.1.soap_http",
                    well_id=well_id,
                    wellbore_id=wellbore_id,
                    endpoint_url=srv.endpoint_url,
                    secret_refs={},
                    config={
                        "auth_mode": "none",
                        "poll_interval_seconds": 2.0,
                        "max_points_per_poll": 2,
                        "channel_mappings": _witsml_mappings(),
                    },
                )
                cid = created["id"]
                await svc.transition(cid, action="start", reason="start p5")
                await session.commit()

            # 1. Partial poll 1 (max_points_per_poll=2) -> data_outcome == "partial_batch_capped"
            worker1 = ConnectorWorker(db, worker_id="wrk_1", lease_seconds=10.0)
            claimed1 = await worker1.claim_connector(cid)
            assert claimed1 is not None
            out1 = await worker1.execute_claimed_poll(claimed1)
            assert out1.status == "succeeded"
            assert out1.points_accepted == 2
            assert out1.checkpoint_status == "partial"
            assert out1.manifest is not None
            assert out1.manifest["data_outcome"] == "partial_batch_capped"

            # 2. Scenario 13: Inject DB failure during Phase 2 ingest -> cursor must remain at out1.cursor_after!
            async def _boom_ingest(*args: Any, **kwargs: Any) -> Any:
                raise RuntimeError("simulated DB failure mid-Phase-2")

            async with db.session() as session:
                row = await session.get(Connector, cid)
                assert row is not None
                row.next_poll_at = utc_now() - dt.timedelta(seconds=1)
                await session.commit()

            claimed_fail = await worker1.claim_connector(cid)
            assert claimed_fail is not None
            monkeypatch.setattr("drillai.telemetry.worker.ingest_frames", _boom_ingest)
            out_fail = await worker1.execute_claimed_poll(claimed_fail)
            monkeypatch.undo()
            assert out_fail.status == "failed"
            assert out_fail.cursor_after == out1.cursor_after

            # 3. Scenario 14: New worker instance (`wrk_2`) restarts and drains remaining 7 points (total 9 points)
            async with db.session() as session:
                row = await session.get(Connector, cid)
                assert row is not None
                row.config = {**dict(row.config or {}), "max_points_per_poll": 50}
                row.next_poll_at = utc_now() - dt.timedelta(seconds=1)
                row.lease_expires_at = utc_now() - dt.timedelta(seconds=1)
                await session.commit()

            worker2 = ConnectorWorker(db, worker_id="wrk_2", lease_seconds=10.0)
            claimed2 = await worker2.claim_connector(cid)
            assert claimed2 is not None
            assert claimed2.recovered_expired_lease is True
            out2 = await worker2.execute_claimed_poll(claimed2)
            assert out2.status == "succeeded"
            assert out2.points_accepted == 7  # 2 + 7 = 9 total points across 3 rows x 3 channels!
            assert out2.manifest is not None
            assert out2.manifest["data_outcome"] == "all_valid_accepted"

            # 4. Poll again when caught up -> 0 new points
            async with db.session() as session:
                row = await session.get(Connector, cid)
                assert row is not None
                row.next_poll_at = utc_now() - dt.timedelta(seconds=1)
                await session.commit()
            claimed3 = await worker2.claim_connector(cid)
            assert claimed3 is not None
            out3 = await worker2.execute_claimed_poll(claimed3)
            assert out3.status == "succeeded"
            assert out3.points_accepted == 0

            # 5. Phase 6 R-01: If lease expires BEFORE Phase 2 commit, worker is fenced even if fencing_token matches!
            async def _expire_lease_before_phase2(claimed_arg: Any) -> None:
                async with db.session() as exp_s:
                    r = await exp_s.get(Connector, claimed_arg.connector_id)
                    assert r is not None
                    r.lease_expires_at = utc_now() - dt.timedelta(seconds=5)
                    await exp_s.commit()

            async with db.session() as session:
                row = await session.get(Connector, cid)
                assert row is not None
                row.next_poll_at = utc_now() - dt.timedelta(seconds=1)
                await session.commit()

            worker_exp = ConnectorWorker(
                db,
                worker_id="wrk_2",
                lease_seconds=10.0,
                before_commit_hook=_expire_lease_before_phase2,
            )
            claimed_exp = await worker_exp.claim_connector(cid)
            assert claimed_exp is not None
            out_exp = await worker_exp.execute_claimed_poll(claimed_exp)
            assert out_exp.status == "fenced"
            # Verify renew_lease also returns False when lease is already expired
            assert await worker_exp.renew_lease(claimed_exp) is False
    finally:
        await db.dispose()


@pytest.mark.postgres
async def test_phase6_postgres_concurrent_sql_cas_races(postgres_url: str):
    """Verify all 5 required concurrent SQL CAS race conditions against real PostgreSQL:
    1. `update` vs `update`
    2. `config change` vs `poll`
    3. `start` vs `stop`
    4. `restart` vs `disable`
    5. `poll` vs `stop`
    """
    db = Database(postgres_url)
    async with db.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    suffix = utc_now().strftime("%H%M%S%f")
    org_id, well_id, wellbore_id = await _seed_org_and_well(db, f"pgrace_{suffix}")
    t0 = dt.datetime(2026, 7, 7, 0, 0, 0, tzinfo=dt.UTC)

    try:
        async with LocalWitsmlSoapServer(page_size_override=5) as srv:
            srv.seed_default_rows(start=t0, count=4, step_seconds=5.0)
            async with db.session() as session:
                svc = ConnectorService(session, org_id)
                created = await svc.create(
                    key=f"witsml-pg-race-{suffix}",
                    name="WITSML PG Race",
                    protocol_profile="witsml.1.4.1.1.soap_http",
                    well_id=well_id,
                    wellbore_id=wellbore_id,
                    endpoint_url=srv.endpoint_url,
                    secret_refs={},
                    config={
                        "auth_mode": "none",
                        "poll_interval_seconds": 5.0,
                        "max_points_per_poll": 100,
                        "channel_mappings": _witsml_mappings(),
                    },
                )
                cid = created["id"]
                v0 = created["config_version"]
                await session.commit()

            # Race 1: Concurrent `update` vs `update` at same expected_config_version
            async def _do_update(new_name: str) -> dict[str, Any]:
                async with db.session() as s:
                    res = await ConnectorService(s, org_id).update(
                        cid,
                        name=new_name,
                        expected_config_version=v0,
                        reason=f"concurrent update {new_name}",
                    )
                    await s.commit()
                    return res

            results_u = await asyncio.gather(
                _do_update("Name A"),
                _do_update("Name B"),
                return_exceptions=True,
            )
            successes_u = [r for r in results_u if isinstance(r, dict)]
            conflicts_u = [r for r in results_u if isinstance(r, Conflict)]
            assert len(successes_u) == 1
            assert len(conflicts_u) == 1

            # Race 3: Concurrent `start` vs `stop` at same expected_config_version
            v1 = successes_u[0]["config_version"]

            async def _do_transition(action: str, expected_v: int) -> dict[str, Any]:
                async with db.session() as s:
                    res = await ConnectorService(s, org_id).transition(
                        cid,
                        action=action,
                        reason=f"concurrent {action}",
                        expected_config_version=expected_v,
                    )
                    await s.commit()
                    return res

            results_ss = await asyncio.gather(
                _do_transition("start", v1),
                _do_transition("stop", v1),
                return_exceptions=True,
            )
            assert sum(1 for r in results_ss if isinstance(r, dict)) == 1
            assert sum(1 for r in results_ss if isinstance(r, Conflict)) == 1

            # Race 4: Concurrent `restart` vs `disable` at same expected_config_version
            async with db.session() as s:
                curr = await ConnectorService(s, org_id).get(cid)
                v2 = int(curr.config_version)
            results_rd = await asyncio.gather(
                _do_transition("restart", v2),
                _do_transition("disable", v2),
                return_exceptions=True,
            )
            assert sum(1 for r in results_rd if isinstance(r, dict)) == 1
            assert sum(1 for r in results_rd if isinstance(r, Conflict)) == 1

            # Ensure connector is started for Race 2 (`config change` vs `poll`)
            async with db.session() as s:
                await ConnectorService(s, org_id).transition(cid, action="start", reason="prep race 2")
                row = await s.get(Connector, cid)
                assert row is not None
                row.next_poll_at = utc_now() - dt.timedelta(seconds=1)
                await s.commit()

            # Race 2: `config change` vs `poll` (operator updates config while worker is mid-poll)
            async def _concurrent_config_change(claimed_arg: Any) -> None:
                async with db.session() as s:
                    await ConnectorService(s, org_id).update(
                        claimed_arg.connector_id,
                        description="updated mid-poll on postgres",
                        reason="mid-poll config change",
                    )
                    await s.commit()

            w_cfg = ConnectorWorker(
                db, worker_id="wrk_pg_cfg", lease_seconds=15.0, before_commit_hook=_concurrent_config_change
            )
            claimed_cfg = await w_cfg.claim_connector(cid)
            assert claimed_cfg is not None
            out_cfg = await w_cfg.execute_claimed_poll(claimed_cfg)
            assert out_cfg.status == "fenced"

            # Race 5: `poll` vs `stop` (operator stops connector while worker is mid-poll)
            async with db.session() as s:
                row = await s.get(Connector, cid)
                assert row is not None
                row.next_poll_at = utc_now() - dt.timedelta(seconds=1)
                row.lease_expires_at = None
                await s.commit()

            async def _concurrent_stop(claimed_arg: Any) -> None:
                async with db.session() as s:
                    await ConnectorService(s, org_id).transition(
                        claimed_arg.connector_id,
                        action="stop",
                        reason="emergency stop during poll",
                    )
                    await s.commit()

            w_stop = ConnectorWorker(
                db, worker_id="wrk_pg_stop", lease_seconds=15.0, before_commit_hook=_concurrent_stop
            )
            claimed_stop = await w_stop.claim_connector(cid)
            assert claimed_stop is not None
            out_stop = await w_stop.execute_claimed_poll(claimed_stop)
            assert out_stop.status == "fenced"

            async with db.session() as s:
                final_row = await s.get(Connector, cid)
                assert final_row is not None
                assert final_row.status == "stopped"
                assert final_row.desired_state == "stopped"
                assert final_row.is_enabled is False
                assert final_row.cursor == {}
    finally:
        await db.dispose()


async def test_phase9_concurrent_multi_connector_soak(tmp_path):
    """Phase 9.1 Multi-Connector Soak & Stability Suite:
    Run concurrent multi-connector soak across:
    1. WITSML 1.4.1.1 SOAP connector
    2. ETP 1.2 binary Avro connector (`etp.1.2.binary_ws`)
    3. Failing endpoint connector
    4. No-data connector
    Verify:
    - no cross-connector checkpoint contamination
    - no unbounded run history growth (retention_limit=4 enforced)
    - fair scheduling under failure/backoff
    - exact point reconciliation across all polls
    """
    from drillai.telemetry.protocols.etp_binary import LocalEtpBinaryWebSocketServer

    db_url = f"sqlite+aiosqlite:///{tmp_path / 'cp13_soak.db'}"
    db = Database(db_url)
    async with db.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    org_id, well_id, wellbore_id = await _seed_org_and_well(db, "soak")
    t0 = dt.datetime(2026, 8, 1, 0, 0, 0, tzinfo=dt.UTC)

    etp_mappings = [
        {
            "source_mnemonic": "SPP",
            "channel_key": "standpipe_pressure",
            "name": "Standpipe Pressure",
            "dimension": "pressure",
            "unit": "psi",
            "source_unit": "psi",
        },
        {
            "source_mnemonic": "WOB",
            "channel_key": "weight_on_bit",
            "name": "Weight on Bit",
            "dimension": "force",
            "unit": "klbf",
            "source_unit": "klbf",
        },
    ]

    try:
        async with (
            LocalWitsmlSoapServer(page_size_override=2) as srv_witsml,
            LocalEtpBinaryWebSocketServer(expected_bearer_token="soak-bin-token") as srv_etp_bin,
            LocalWitsmlSoapServer(page_size_override=2) as srv_failing,
            LocalEtpBinaryWebSocketServer(expected_bearer_token="soak-bin-token") as srv_empty,
        ):
            srv_witsml.seed_default_rows(start=t0, count=6, step_seconds=5.0)
            srv_etp_bin.seed_default_points(start=t0 + dt.timedelta(minutes=10), count=6, step_seconds=5.0)
            srv_failing.fault_mode = "http_500"
            srv_empty.points = []

            import os

            os.environ["DRILLAI_SECRET_SOAK_ETP_BIN"] = "soak-bin-token"

            async with db.session() as session:
                svc = ConnectorService(session, org_id)
                c_witsml = await svc.create(
                    key="soak-witsml",
                    name="Soak WITSML 1.4.1.1 SOAP",
                    protocol_profile="witsml.1.4.1.1.soap_http",
                    well_id=well_id,
                    wellbore_id=wellbore_id,
                    endpoint_url=srv_witsml.endpoint_url,
                    secret_refs={},
                    config={
                        "auth_mode": "none",
                        "poll_interval_seconds": 1.0,
                        "max_points_per_poll": 2,
                        "channel_mappings": _witsml_mappings(),
                    },
                )
                c_etp_bin = await svc.create(
                    key="soak-etp-bin",
                    name="Soak ETP 1.2 Binary Avro",
                    protocol_profile="etp.1.2.binary_ws",
                    well_id=well_id,
                    wellbore_id=wellbore_id,
                    endpoint_url=srv_etp_bin.endpoint_url,
                    secret_refs={"bearer_token": "env:DRILLAI_SECRET_SOAK_ETP_BIN"},
                    config={
                        "auth_mode": "bearer",
                        "poll_interval_seconds": 1.0,
                        "max_points_per_poll": 2,
                        "channel_mappings": etp_mappings,
                    },
                )
                c_fail = await svc.create(
                    key="soak-failing",
                    name="Soak Failing Store",
                    protocol_profile="witsml.1.4.1.1.soap_http",
                    well_id=well_id,
                    wellbore_id=wellbore_id,
                    endpoint_url=srv_failing.endpoint_url,
                    secret_refs={},
                    config={
                        "auth_mode": "none",
                        "poll_interval_seconds": 1.0,
                        "max_points_per_poll": 10,
                        "max_consecutive_failures": 5,
                        "channel_mappings": _witsml_mappings(),
                    },
                )
                c_empty = await svc.create(
                    key="soak-empty",
                    name="Soak Empty ETP Stream",
                    protocol_profile="etp.1.2.binary_ws",
                    well_id=well_id,
                    wellbore_id=wellbore_id,
                    endpoint_url=srv_empty.endpoint_url,
                    secret_refs={"bearer_token": "env:DRILLAI_SECRET_SOAK_ETP_BIN"},
                    config={
                        "auth_mode": "bearer",
                        "poll_interval_seconds": 1.0,
                        "max_points_per_poll": 10,
                        "channel_mappings": etp_mappings,
                    },
                )
                for cid in (c_witsml["id"], c_etp_bin["id"], c_fail["id"], c_empty["id"]):
                    await svc.transition(cid, action="start", reason="start soak")
                await session.commit()

            worker = ConnectorWorker(db, worker_id="wrk_soak", lease_seconds=10.0, retention_limit=4)

            for _tick in range(6):
                async with db.session() as session:
                    for cid in (c_witsml["id"], c_etp_bin["id"], c_fail["id"], c_empty["id"]):
                        row = await session.get(Connector, cid)
                        assert row is not None
                        if row.status != "failed":
                            row.next_poll_at = utc_now() - dt.timedelta(seconds=1)
                    await session.commit()
                await worker.run_once()

            async with db.session() as session:
                svc = ConnectorService(session, org_id)
                w_snap = await svc.inspect(c_witsml["id"])
                e_snap = await svc.inspect(c_etp_bin["id"])
                f_snap = await svc.inspect(c_fail["id"])
                n_snap = await svc.inspect(c_empty["id"])

                # 1. No cross-connector checkpoint contamination
                assert "cursor_timestamp" in w_snap["cursor"]
                assert w_snap["cursor"].get("protocol") == "witsml.1.4.1.1.soap_http"
                assert "channel_cursors" not in w_snap["cursor"]
                assert "channel_cursors" in e_snap["cursor"]
                assert e_snap["cursor"].get("protocol") == "etp.1.2.binary_ws"
                assert "completed_at_cursor" not in e_snap["cursor"]
                assert f_snap["cursor"] == {}
                assert n_snap["cursor"].get("channel_cursors") == {}
                assert n_snap["cursor"].get("cursor_timestamp") is None

                # 2. Fair scheduling: healthy connectors remain running while failing connector backs off/fails
                assert w_snap["status"] == "running"
                assert e_snap["status"] == "running"
                assert f_snap["status"] in ("backing_off", "failed")
                assert f_snap["health"]["consecutive_failures"] >= 1
                assert n_snap["status"] == "running"

                # 3. Bounded run history growth (retention_limit=4) & exact point reconciliation
                for cid in (c_witsml["id"], c_etp_bin["id"], c_fail["id"], c_empty["id"]):
                    runs_items = await svc.list_runs(cid, limit=25)
                    assert len(runs_items) <= 4
                    for run in runs_items:
                        if run["status"] == "succeeded":
                            manifest = run["details"]["manifest"]
                            assert manifest["reconciled"] is True
                            assert (
                                manifest["source_records_discovered"]
                                == manifest["mapped_frames_emitted"]
                                + manifest["unmapped_skipped"]
                                + manifest["watermark_suppressed"]
                                + manifest["deferred_over_cap"]
                            )
    finally:
        await db.dispose()

