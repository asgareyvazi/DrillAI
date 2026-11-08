"""CP130 connector lease checks with truly separate database engines and worker sessions."""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest
from sqlalchemy import select

from drillai.core.clock import utc_now
from drillai.db.models import Connector, Organization
from drillai.db.session import Database
from drillai.telemetry.worker import ConnectorWorker


def _connector(org_id: str, key: str) -> Connector:
    return Connector(
        org_id=org_id,
        key=key,
        name="Synthetic independent-worker connector",
        provider="synthetic",
        protocol_profile="synthetic.v1",
        direction="inbound",
        status="configured",
        desired_state="enabled",
        config_version=1,
        config={},
        secret_refs={},
        capabilities=[],
        cursor={"checkpoint": "before"},
        fencing_token=0,
        error_count=0,
        reconnect_count=0,
        backoff_seconds=0.0,
        is_enabled=True,
    )


async def _seed(database: Database) -> tuple[str, str]:
    identity = uuid4().hex
    async with database.session() as session:
        org = Organization(slug=f"cp130-connector-{identity}", name="CP130 synthetic connector org")
        session.add(org)
        await session.flush()
        connector = _connector(org.id, f"cp130-connector-{identity}")
        session.add(connector)
        await session.flush()
        return org.id, connector.id


async def test_independent_sqlite_engines_observe_the_durable_connector_lease(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path / 'independent-workers.db'}"
    database_a = Database(url)
    database_b = Database(url)
    try:
        await database_a.create_all()
        org_id, connector_id = await _seed(database_a)
        assert database_a.engine is not database_b.engine

        worker_a = ConnectorWorker(database_a, worker_id="cp130-worker-a", lease_seconds=10.0)
        worker_b = ConnectorWorker(database_b, worker_id="cp130-worker-b", lease_seconds=10.0)
        claim_a = await worker_a.claim_connector(connector_id, now=utc_now())
        claim_b = await worker_b.claim_connector(connector_id, now=utc_now())

        assert claim_a is not None
        assert claim_b is None
        assert claim_a.fencing_token == 1
        async with database_b.session() as session:
            persisted = (
                await session.execute(select(Connector).where(Connector.id == connector_id))
            ).scalar_one()
        assert persisted.org_id == org_id
        assert persisted.worker_id == "cp130-worker-a"
        assert persisted.fencing_token == claim_a.fencing_token
        assert persisted.cursor == {"checkpoint": "before"}
    finally:
        await database_a.dispose()
        await database_b.dispose()


@pytest.mark.postgres
async def test_postgres_competing_workers_with_independent_engines_have_one_cas_winner(postgres_url):
    url = postgres_url
    database_a = Database(url)
    database_b = Database(url)
    try:
        await database_a.create_all()
        _org_id, connector_id = await _seed(database_a)
        assert database_a.engine is not database_b.engine

        worker_a = ConnectorWorker(database_a, worker_id="cp130-pg-worker-a", lease_seconds=10.0)
        worker_b = ConnectorWorker(database_b, worker_id="cp130-pg-worker-b", lease_seconds=10.0)
        start_together = asyncio.Barrier(2)

        async def claim(worker: ConnectorWorker):
            await start_together.wait()
            return await worker.claim_connector(connector_id, now=utc_now())

        claims = await asyncio.gather(claim(worker_a), claim(worker_b))
        winners = [claim_result for claim_result in claims if claim_result is not None]
        assert len(winners) == 1
        winner = winners[0]
        async with database_b.session() as session:
            persisted = (
                await session.execute(select(Connector).where(Connector.id == connector_id))
            ).scalar_one()
        assert persisted.fencing_token == winner.fencing_token == 1
        assert persisted.worker_id in {"cp130-pg-worker-a", "cp130-pg-worker-b"}
        assert persisted.lease_expires_at is not None
    finally:
        await database_a.dispose()
        await database_b.dispose()
