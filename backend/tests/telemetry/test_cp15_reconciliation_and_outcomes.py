"""CP15 Phase 3 & Phase 4 — Fail-Closed Manifest Reconciliation Transaction Invariant
and Mathematical `data_outcome` Truth Table / Dry-Run Semantics Tests.
"""

from __future__ import annotations

import datetime as dt
import itertools
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select

from drillai.core.config import reset_settings_cache
from drillai.core.errors import ValidationFailed
from drillai.db.base import Base, utc_now
from drillai.db.models import Connector, ConnectorRun, Project, TimeSeriesPoint, Well
from drillai.db.session import Database
from drillai.security.actions import Principal
from drillai.telemetry.connectors import (
    POLL_DATA_OUTCOMES,
    ConnectorService,
    build_and_reconcile_poll_manifest,
    clear_harness_endpoints,
    compute_poll_data_outcome,
    register_harness_endpoint,
    reset_egress_policy,
    set_custom_dns_resolver,
)
from drillai.telemetry.protocols import LocalEtpBinaryWebSocketServer, LocalWitsmlSoapServer
from drillai.telemetry.worker import ConnectorWorker

DEFAULT_ORG_ID = "org_cp15_reconcile"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DRILLAI_ENVIRONMENT", "test")
    monkeypatch.setenv("DRILLAI_AUTH_ENABLED", "false")
    monkeypatch.setenv("DRILLAI_CONNECTOR_ALLOW_LOOPBACK", "true")
    monkeypatch.setenv("DRILLAI_SECRET_CP15_TOK", "tok-cp15-rec")
    reset_settings_cache()
    reset_egress_policy()
    clear_harness_endpoints()
    set_custom_dns_resolver(None)
    yield
    reset_egress_policy()
    clear_harness_endpoints()
    set_custom_dns_resolver(None)
    reset_settings_cache()


async def _seed_well(db: Database) -> str:
    async with db.session() as session:
        proj = Project(
            id="prj_cp15_rec",
            org_id=DEFAULT_ORG_ID,
            code="CP15-REC",
            name="CP15 Reconciliation Project",
        )
        well = Well(
            id="well_cp15_rec",
            org_id=DEFAULT_ORG_ID,
            project_id=proj.id,
            name="CP15 Reconciliation Well",
            uwi="UWI-CP15-REC-001",
        )
        session.add_all([proj, well])
        await session.commit()
        return well.id


def test_reconciliation_rejects_booleans_negatives_per_channel_and_cross_sum_mismatches() -> None:
    """Scenarios 3..12: Verify strict non-negative integer checking, global & per-channel conservation,
    per-channel cross-sum equality against global totals, explicit total_filtered/total_protocol_rejected
    consistency, and dry-run non-persistence invariants.
    """
    # 1. Valid baseline with per_source_channel matching global totals
    valid_disp = {
        "pages_or_messages_received": 1,
        "source_records_discovered": 5,
        "mapped_frames_emitted": 3,
        "unmapped_skipped": 1,
        "unsubscribed_skipped": 0,
        "watermark_suppressed": 1,
        "in_batch_duplicates": 0,
        "deferred_over_cap": 0,
        "rejected_malformed_timestamp": 0,
        "rejected_unsupported_index": 0,
        "rejected_invalid_value": 0,
        "rejected_non_finite_value": 0,
        "rejected_unknown_channel": 0,
        "per_source_channel": {
            "HKLD": {
                "discovered": 4,
                "emitted": 3,
                "unmapped_skipped": 0,
                "unsubscribed_skipped": 0,
                "watermark_suppressed": 1,
                "in_batch_duplicates": 0,
                "deferred_over_cap": 0,
                "rejected": 0,
            },
            "UNMAPPED_CH": {
                "discovered": 1,
                "emitted": 0,
                "unmapped_skipped": 1,
                "unsubscribed_skipped": 0,
                "watermark_suppressed": 0,
                "in_batch_duplicates": 0,
                "deferred_over_cap": 0,
                "rejected": 0,
            },
        },
    }
    m_valid = build_and_reconcile_poll_manifest(
        protocol_disp=valid_disp,
        protocol_dups=1,
        frames_received=3,
        accepted=2,
        service_duplicates=0,
        revised=1,
        service_rejected=0,
        has_more_deferred=False,
        checkpoint_status="complete",
    )
    assert m_valid["protocol_reconciled"] is True
    assert m_valid["ingestion_reconciled"] is True
    assert m_valid["reconciled"] is True
    assert m_valid["persisted"] is True
    assert m_valid["filtered_present"] is True
    assert m_valid["revised_present"] is True
    assert m_valid["duplicates_present"] is True
    assert m_valid["data_outcome"] == "all_valid_accepted"

    # 2. Per-channel cross-sum mismatch: each channel internally sums to its `discovered`,
    # but the sum of channels (4) != global `source_records_discovered` (5)
    cross_sum_bad = {
        **valid_disp,
        "per_source_channel": {
            "HKLD": {
                "discovered": 4,
                "emitted": 3,
                "unmapped_skipped": 0,
                "unsubscribed_skipped": 0,
                "watermark_suppressed": 1,
                "in_batch_duplicates": 0,
                "deferred_over_cap": 0,
                "rejected": 0,
            }
        },
    }
    m_cross_bad = build_and_reconcile_poll_manifest(
        protocol_disp=cross_sum_bad,
        protocol_dups=1,
        frames_received=3,
        accepted=2,
        service_duplicates=0,
        revised=1,
        service_rejected=0,
        has_more_deferred=False,
        checkpoint_status="complete",
    )
    assert m_cross_bad["protocol_reconciled"] is False
    assert m_cross_bad["reconciled"] is False
    assert m_cross_bad["data_outcome"] == "reconciliation_error"
    assert m_cross_bad["persisted"] is False
    assert m_cross_bad["rolled_back"] is True

    # 3. Boolean `True` in protocol count field (since isinstance(True, int) is True in Python!)
    bool_in_proto = {
        **valid_disp,
        "unmapped_skipped": True,
    }
    m_bool_proto = build_and_reconcile_poll_manifest(
        protocol_disp=bool_in_proto,
        protocol_dups=1,
        frames_received=3,
        accepted=2,
        service_duplicates=0,
        revised=1,
        service_rejected=0,
        has_more_deferred=False,
        checkpoint_status="complete",
    )
    assert m_bool_proto["protocol_reconciled"] is False
    assert m_bool_proto["reconciled"] is False
    assert m_bool_proto["data_outcome"] == "reconciliation_error"

    # 4. Boolean `True` or negative int in ingestion count field
    for bad_accepted in (True, False, -1, 1.5, "2", None):
        m_bad_ing_type = build_and_reconcile_poll_manifest(
            protocol_disp=valid_disp,
            protocol_dups=1,
            frames_received=3,
            accepted=bad_accepted,  # type: ignore[arg-type]
            service_duplicates=0,
            revised=1,
            service_rejected=0,
            has_more_deferred=False,
            checkpoint_status="complete",
        )
        assert m_bad_ing_type["ingestion_reconciled"] is False
        assert m_bad_ing_type["reconciled"] is False
        assert m_bad_ing_type["data_outcome"] == "reconciliation_error"

    # 5. Negative integer in protocol count field (even if algebraic sum matches!)
    neg_proto = {
        "pages_or_messages_received": 1,
        "source_records_discovered": 2,
        "mapped_frames_emitted": 3,
        "unmapped_skipped": -1,
        "unsubscribed_skipped": 0,
        "watermark_suppressed": 0,
        "in_batch_duplicates": 0,
        "deferred_over_cap": 0,
        "rejected_malformed_timestamp": 0,
        "rejected_unsupported_index": 0,
        "rejected_invalid_value": 0,
        "rejected_non_finite_value": 0,
        "rejected_unknown_channel": 0,
        "per_source_channel": {},
    }
    m_neg_proto = build_and_reconcile_poll_manifest(
        protocol_disp=neg_proto,
        protocol_dups=0,
        frames_received=3,
        accepted=3,
        service_duplicates=0,
        revised=0,
        service_rejected=0,
        has_more_deferred=False,
        checkpoint_status="complete",
    )
    assert m_neg_proto["protocol_reconciled"] is False
    assert m_neg_proto["reconciled"] is False
    assert m_neg_proto["data_outcome"] == "reconciliation_error"

    # 6. Explicit total_filtered or total_protocol_rejected mismatch in dict disposition
    m_bad_explicit_tot = build_and_reconcile_poll_manifest(
        protocol_disp={**valid_disp, "total_filtered": 99},
        protocol_dups=1,
        frames_received=3,
        accepted=2,
        service_duplicates=0,
        revised=1,
        service_rejected=0,
        has_more_deferred=False,
        checkpoint_status="complete",
    )
    assert m_bad_explicit_tot["protocol_reconciled"] is False
    assert m_bad_explicit_tot["reconciled"] is False
    assert m_bad_explicit_tot["data_outcome"] == "reconciliation_error"

    # 7. Dry-run claiming non-zero accepted points must fail ingestion_reconciled
    m_bad_dry = build_and_reconcile_poll_manifest(
        protocol_disp=valid_disp,
        protocol_dups=1,
        frames_received=3,
        accepted=3,  # Illegal when dry_run=True!
        service_duplicates=0,
        revised=0,
        service_rejected=0,
        has_more_deferred=False,
        checkpoint_status="unchanged",
        dry_run=True,
    )
    assert m_bad_dry["ingestion_reconciled"] is False
    assert m_bad_dry["reconciled"] is False
    assert m_bad_dry["data_outcome"] == "reconciliation_error"


def test_exhaustive_cross_product_data_outcome_truth_table() -> None:
    """Phase 4.4: Exhaustive cross-product test over the `compute_poll_data_outcome` truth table."""
    valid_outcomes = set(POLL_DATA_OUTCOMES)
    seen: set[str] = set()

    for (
        reconciled,
        dry_run,
        has_more_deferred,
        accepted,
        revised,
        dups,
        rejected,
        filtered,
        frames,
    ) in itertools.product(
        [True, False],
        [False, True],
        [False, True],
        [0, 2],
        [0, 1],
        [0, 3],
        [0, 1],
        [0, 4],
        [0, 2],
    ):
        discovered = frames + dups + rejected + filtered
        outcome = compute_poll_data_outcome(
            source_records_discovered=discovered,
            frames_received=frames,
            points_accepted=accepted,
            points_duplicates=dups,
            points_revised=revised,
            points_rejected=rejected,
            total_filtered=filtered,
            has_more_deferred=has_more_deferred,
            dry_run=dry_run,
            reconciled=reconciled,
        )
        assert outcome in valid_outcomes
        seen.add(outcome)

        # Invariant 1: reconciled=False ALWAYS yields reconciliation_error
        if not reconciled:
            assert outcome == "reconciliation_error"
            continue

        # Invariant 2: all_valid_accepted NEVER occurs when points_accepted == 0 or dry_run is True or rejected > 0
        if outcome == "all_valid_accepted":
            assert accepted > 0
            assert not dry_run
            assert rejected == 0
            assert not has_more_deferred

        # Invariant 3: dry_run_preview occurs only when dry_run=True, frames > 0, rejected == 0, not has_more_deferred
        if outcome == "dry_run_preview":
            assert dry_run is True
            assert frames > 0
            assert rejected == 0
            assert not has_more_deferred

        # Invariant 4: revisions_only vs revisions_and_duplicates vs duplicates_only when accepted == 0, rejected == 0, not capped, not dry_run
        if (
            not dry_run
            and accepted == 0
            and rejected == 0
            and not has_more_deferred
        ):
            if revised > 0 and dups > 0:
                assert outcome == "revisions_and_duplicates"
            elif revised > 0 and dups == 0:
                assert outcome == "revisions_only"
            elif revised == 0 and dups > 0 and frames > 0:
                assert outcome == "duplicates_only"

    assert seen == valid_outcomes


@pytest.mark.asyncio
async def test_worker_poll_and_replay_rollback_telemetry_and_cursor_on_reconciliation_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scenarios 1, 2, 13, 14, 15, 16, 17, 18, 19:
    Prove on a real database that whenever `reconciled=False` (either protocol disposition corruption
    or ingestion count mismatch):
    - `ConnectorWorker.execute_claimed_poll()` rolls back all `TimeSeriesPoint` inserts in the transaction,
      leaves `Connector.cursor` at `cursor_before`, transitions `Connector` to `backing_off`, and records
      `ConnectorRun(status="failed", error_category="reconciliation_error", points_accepted=0)` with
      `manifest.data_outcome == "reconciliation_error"`.
    - `ConnectorService.replay(..., dry_run=False)` rolls back its nested savepoint so 0 new `TimeSeriesPoint`
      rows survive, preserves `cursor_before`, and records `ConnectorRun(status="failed", error_category="reconciliation_error")`.
    - `ConnectorService.replay(..., dry_run=True)` records `status="failed"`, `error_category="reconciliation_error"`.
    - `ConnectorService.preview()` raises `ValidationFailed(reason="reconciliation_error")`.
    - `ConnectorService.test_connection()` fails at `disposition_validated` with `error_category="reconciliation_error"`.
    - Subsequent healthy worker poll recovers from `cursor_before` with zero data loss.
    """
    db_url = f"sqlite+aiosqlite:///{tmp_path / 'cp15_reconcile_tx.db'}"
    db = Database(db_url)
    async with db.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    well_id = await _seed_well(db)
    principal = Principal(id="eng-cp15-rec", org_id=DEFAULT_ORG_ID, role_keys=("admin", "engineer"))

    witsml_server = LocalWitsmlSoapServer(expected_bearer_token="tok-cp15-rec")
    await witsml_server.start()
    register_harness_endpoint(witsml_server.host, witsml_server.port)

    etp_server = LocalEtpBinaryWebSocketServer(expected_bearer_token="tok-cp15-rec")
    await etp_server.start()
    register_harness_endpoint(etp_server.host, etp_server.port)

    try:
        async with db.session() as session:
            svc = ConnectorService(session, DEFAULT_ORG_ID, principal=principal)
            created = await svc.create(
                key="witsml-reconcile-tx",
                name="WITSML Reconcile TX",
                protocol_profile="witsml.1.4.1.1.soap_http",
                well_id=well_id,
                endpoint_url=witsml_server.endpoint_url,
                config={
                    "auth_mode": "bearer",
                    "tls_verify": True,
                    "poll_interval_seconds": 0.1,
                    "channel_mappings": [
                        {
                            "source_mnemonic": "HKLD",
                            "channel_key": "hookload",
                            "name": "Hookload",
                            "dimension": "force",
                            "unit": "klbf",
                        }
                    ],
                },
                secret_refs={"bearer_token": "env:DRILLAI_SECRET_CP15_TOK"},
            )
            cid = created["id"]
            await svc.transition(cid, action="start", reason="Start WITSML connector for TX test")
            await session.commit()

        # Inject a corrupted protocol disposition via _before_commit_hook so adapter.last_batch.disposition
        # has source_records_discovered=999 while ingest_frames() actually inserts real TimeSeriesPoint rows
        # into the Phase 2 write session BEFORE build_and_reconcile_poll_manifest() runs!
        async def _corrupt_protocol_disposition(claimed_arg: Any) -> None:
            pass

        orig_build = ConnectorService.build_adapter

        async def _patched_build_adapter(self_svc: Any, row_arg: Any, **kwargs: Any) -> Any:
            adapter = await orig_build(self_svc, row_arg, **kwargs)
            orig_connect = adapter.connect
            orig_poll = adapter.poll

            async def _corrupted_connect() -> None:
                await orig_connect()
                if getattr(adapter, "last_batch", None) is not None:
                    adapter.last_batch.disposition.source_records_discovered = 999

            async def _corrupted_poll() -> Any:
                frames = await orig_poll()
                if getattr(adapter, "last_batch", None) is not None:
                    adapter.last_batch.disposition.source_records_discovered = 999
                return frames

            adapter.connect = _corrupted_connect
            adapter.poll = _corrupted_poll
            return adapter

        monkeypatch.setattr(ConnectorService, "build_adapter", _patched_build_adapter)

        # 1. test_connection() with corrupted disposition fails at disposition_validated
        async with db.session() as session:
            svc = ConnectorService(session, DEFAULT_ORG_ID, principal=principal)
            tc_res = await svc.test_connection(cid)
            await session.commit()
        assert tc_res["ok"] is False
        assert tc_res["error_category"] == "reconciliation_error"
        by_stage = {m["stage"]: m["status"] for m in tc_res["milestones"]}
        assert by_stage["data_frame_decoded"] == "passed"
        assert by_stage["disposition_validated"] == "failed"
        assert by_stage["transaction_committed"] == "not_applicable"

        # 2. preview() with corrupted disposition raises ValidationFailed(reason="reconciliation_error")
        async with db.session() as session:
            svc = ConnectorService(session, DEFAULT_ORG_ID, principal=principal)
            with pytest.raises(ValidationFailed) as exc_prev:
                await svc.preview(cid)
            assert exc_prev.value.details["reason"] == "reconciliation_error"

        # 3. Worker poll with corrupted disposition: ingest_frames() flushes points into session,
        # then reconciliation check fails -> session.rollback() removes ALL points and preserves cursor!
        worker = ConnectorWorker(db, worker_id="wrk-cp15-rec")
        outcomes_bad = await worker.run_once()
        assert len(outcomes_bad) == 1
        out_bad = outcomes_bad[0]
        assert out_bad.status == "failed"
        assert out_bad.error_category == "reconciliation_error"
        assert out_bad.points_accepted == 0
        assert out_bad.cursor_after == {}
        assert out_bad.manifest is not None
        assert out_bad.manifest["reconciled"] is False
        assert out_bad.manifest["data_outcome"] == "reconciliation_error"
        assert out_bad.manifest["persisted"] is False
        assert out_bad.manifest["rolled_back"] is True

        # Verify in DB: 0 TimeSeriesPoint rows exist, Connector.cursor is still {}, ConnectorRun is status="failed"
        async with db.session() as session:
            pt_count = (
                await session.execute(select(func.count()).select_from(TimeSeriesPoint))
            ).scalar_one()
            assert pt_count == 0

            conn_row = await session.get(Connector, cid)
            assert conn_row is not None
            assert conn_row.cursor == {}
            assert conn_row.status == "backing_off"
            assert conn_row.last_error_category == "reconciliation_error"

            runs = (
                await session.execute(
                    select(ConnectorRun)
                    .where(ConnectorRun.connector_id == cid, ConnectorRun.run_kind == "poll")
                    .order_by(ConnectorRun.started_at.desc())
                )
            ).scalars().all()
            assert len(runs) == 1
            assert runs[0].status == "failed"
            assert runs[0].error_category == "reconciliation_error"
            assert runs[0].points_accepted == 0
            assert runs[0].details["manifest"]["data_outcome"] == "reconciliation_error"
            assert runs[0].details["manifest"]["reconciled"] is False

        # 4. Replay dry_run=True and dry_run=False with corrupted disposition both fail closed and persist 0 points
        async with db.session() as session:
            svc = ConnectorService(session, DEFAULT_ORG_ID, principal=principal)
            dry_res = await svc.replay(
                cid,
                reason="Test dry-run fail-closed reconciliation",
                from_timestamp="2020-01-01T00:00:00Z",
                dry_run=True,
            )
            await session.commit()
            assert dry_res["ok"] is False
            assert dry_res["error_category"] == "reconciliation_error"
            assert dry_res["run"]["status"] == "failed"
            assert dry_res["run"]["details"]["manifest"]["data_outcome"] == "reconciliation_error"

            live_rep_res = await svc.replay(
                cid,
                reason="Test live replay savepoint rollback on reconciliation failure",
                from_timestamp="2020-01-01T00:00:00Z",
                dry_run=False,
            )
            await session.commit()
            assert live_rep_res["ok"] is False
            assert live_rep_res["persisted"] is False
            assert live_rep_res["error_category"] == "reconciliation_error"
            assert live_rep_res["run"]["status"] == "failed"
            assert live_rep_res["run"]["points_accepted"] == 0
            assert live_rep_res["run"]["details"]["manifest"]["data_outcome"] == "reconciliation_error"

            pt_count_after_replay = (
                await session.execute(select(func.count()).select_from(TimeSeriesPoint))
            ).scalar_one()
            assert pt_count_after_replay == 0

        # 5. Restore healthy adapter and verify next worker poll recovers from preserved cursor={} and ingests all points!
        monkeypatch.setattr(ConnectorService, "build_adapter", orig_build)
        async with db.session() as session:
            conn_row = await session.get(Connector, cid)
            assert conn_row is not None
            conn_row.next_poll_at = utc_now() - dt.timedelta(seconds=1)
            conn_row.lease_expires_at = utc_now() - dt.timedelta(seconds=1)
            await session.commit()

        outcomes_ok = await worker.run_once()
        assert len(outcomes_ok) == 1
        assert outcomes_ok[0].status == "succeeded"
        assert outcomes_ok[0].points_accepted == 6
        assert outcomes_ok[0].manifest is not None
        assert outcomes_ok[0].manifest["reconciled"] is True
        assert outcomes_ok[0].manifest["persisted"] is True
        assert outcomes_ok[0].manifest["data_outcome"] == "all_valid_accepted"

        # 6. Verify healthy dry-run replay reports data_outcome == "dry_run_preview" (never "all_valid_accepted")
        async with db.session() as session:
            svc = ConnectorService(session, DEFAULT_ORG_ID, principal=principal)
            healthy_dry = await svc.replay(
                cid,
                reason="Healthy dry-run preview outcome check",
                from_timestamp="2020-01-01T00:00:00Z",
                dry_run=True,
            )
            await session.commit()
            assert healthy_dry["ok"] is True
            assert healthy_dry["dry_run"] is True
            assert healthy_dry["persisted"] is False
            dry_manifest = healthy_dry["run"]["details"]["manifest"]
            assert dry_manifest["reconciled"] is True
            assert dry_manifest["persisted"] is False
            assert dry_manifest["dry_run"] is True
            assert dry_manifest["data_outcome"] == "dry_run_preview"

        # 7. ETP 1.2 Binary Avro connector with ingestion count mismatch (ingest_frames report disagrees with frames_received):
        # verify Phase 2 write session rolls back all inserted TimeSeriesPoint rows and preserves cursor={}
        async with db.session() as session:
            svc = ConnectorService(session, DEFAULT_ORG_ID, principal=principal)
            await svc.transition(cid, action="stop", reason="Stop WITSML connector before ETP TX test")
            etp_created = await svc.create(
                key="etp-bin-reconcile-tx",
                name="ETP Binary Reconcile TX",
                protocol_profile="etp.1.2.binary_ws",
                well_id=well_id,
                endpoint_url=etp_server.endpoint_url,
                config={
                    "auth_mode": "bearer",
                    "tls_verify": True,
                    "poll_interval_seconds": 0.1,
                    "channel_mappings": [
                        {
                            "source_mnemonic": "SPP",
                            "channel_key": "spp",
                            "name": "Standpipe Pressure",
                            "dimension": "pressure",
                            "unit": "psi",
                        }
                    ],
                },
                secret_refs={"bearer_token": "env:DRILLAI_SECRET_CP15_TOK"},
            )
            etp_cid = etp_created["id"]
            await svc.transition(etp_cid, action="start", reason="Start ETP binary connector for TX test")
            await session.commit()

        from drillai.telemetry import worker as worker_mod

        orig_ingest = worker_mod.ingest_frames

        async def _corrupted_ingest(*args: Any, **kwargs: Any) -> Any:
            rep = await orig_ingest(*args, **kwargs)
            # Corrupt the report's frame count after points were flushed into the session
            rep.frames = rep.frames + 5
            return rep

        monkeypatch.setattr(worker_mod, "ingest_frames", _corrupted_ingest)
        outcomes_etp_bad = await worker.run_once()
        assert len(outcomes_etp_bad) == 1
        assert outcomes_etp_bad[0].status == "failed"
        assert outcomes_etp_bad[0].error_category == "reconciliation_error"
        assert outcomes_etp_bad[0].points_accepted == 0
        assert outcomes_etp_bad[0].cursor_after == {}
        assert outcomes_etp_bad[0].manifest is not None
        assert outcomes_etp_bad[0].manifest["protocol_reconciled"] is True
        assert outcomes_etp_bad[0].manifest["ingestion_reconciled"] is False
        assert outcomes_etp_bad[0].manifest["reconciled"] is False
        assert outcomes_etp_bad[0].manifest["data_outcome"] == "reconciliation_error"

        # Verify in DB: still only the 6 WITSML points exist (0 ETP points survived rollback!)
        async with db.session() as session:
            pt_count_etp_bad = (
                await session.execute(select(func.count()).select_from(TimeSeriesPoint))
            ).scalar_one()
            assert pt_count_etp_bad == 6

        # Restore healthy ingest_frames and verify ETP binary poll succeeds on next cycle
        monkeypatch.setattr(worker_mod, "ingest_frames", orig_ingest)
        async with db.session() as session:
            etp_row = await session.get(Connector, etp_cid)
            assert etp_row is not None
            etp_row.next_poll_at = utc_now() - dt.timedelta(seconds=1)
            etp_row.lease_expires_at = utc_now() - dt.timedelta(seconds=1)
            await session.commit()

        outcomes_etp_ok = await worker.run_once()
        assert len(outcomes_etp_ok) == 1
        assert outcomes_etp_ok[0].status == "succeeded"
        assert outcomes_etp_ok[0].points_accepted > 0
        assert outcomes_etp_ok[0].manifest["reconciled"] is True
        assert outcomes_etp_ok[0].manifest["data_outcome"] == "all_valid_accepted"
    finally:
        await witsml_server.stop()
        await etp_server.stop()
        await db.dispose()
