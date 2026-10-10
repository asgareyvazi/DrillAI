"""First requests against an empty database must not race each other into a 500.

A browser screen opens with several queries in flight at once; on a database that has never been used
those queries all try to create the same development organization. The unique constraint on
``organizations.slug`` is correct — the platform must resolve the conflict instead of surfacing an
integrity error, and must not roll back the work the losing request had already done.

The first test reproduces the interleaving deterministically rather than hoping that two event-loop
tasks happen to overlap: the winning row is written in the instant between this session's read and its
insert. That is the exact window the race lives in, and the test fails with an integrity error when the
conflict handling is removed.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from drillai.api.deps import ensure_org
from drillai.db.models import Base, Organization, Project

pytestmark = pytest.mark.asyncio

INSERT_WINNER = (
    "INSERT INTO organizations "
    "(slug, name, kind, country, timezone, default_unit_system, default_locale, settings, is_active, "
    " id, updated_at, created_at) "
    "VALUES ('race-operator', 'Race Operator', 'operator', NULL, 'UTC', 'field_us', 'en', '{}', 1, "
    " 'org_injected_winner', '2026-01-01 00:00:00', '2026-01-01 00:00:00')"
)

@pytest.fixture
async def database(tmp_path):
    """A file-backed database: the race is between *connections*, which in-memory SQLite cannot show."""
    path = tmp_path / "bootstrap.db"
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{path}", connect_args={"timeout": 30}
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield engine, path
    await engine.dispose()


class RacingSession:
    """A session proxy that lets a competing request win the race between read and write.

    The proxy inserts the winner's row immediately after the read that concluded "no such org" and
    before the write that would create it — the exact window the race lives in. It is deterministic
    on any database, unlike a test that hopes two scheduled tasks overlap.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._raced = False
        self.raced = False

    async def execute(self, statement: object, *args: object, **kwargs: object):
        result = await self._session.execute(statement, *args, **kwargs)
        if not self._raced and "organizations" in str(statement):
            self._raced = True
            await self._session.execute(text(INSERT_WINNER))
            await self._session.flush()
            self.raced = True
        return result

    def __getattr__(self, name: str) -> object:
        return getattr(self._session, name)


async def test_a_lost_race_resolves_to_the_org_that_won(database):
    """A request that loses the bootstrap race joins the winner instead of failing with a 500."""
    engine, _path = database
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)

    async with factory() as session:
        racing = RacingSession(session)
        project = Project(org_id="pending", name="Pending project")
        session.add(project)

        org = await ensure_org(racing, "race-operator")  # type: ignore[arg-type]

        assert racing.raced, "the interleaving under test did not happen"
        assert org.slug == "race-operator"
        assert org.id == "org_injected_winner", "the row that won is the row that is used"
        assert session.is_active, "losing the race must not roll the caller's session back"
        await session.commit()

    async with factory() as session:
        projects = (await session.execute(select(Project))).scalars().all()
        orgs = (await session.execute(select(Organization))).scalars().all()
    assert [row.name for row in projects] == ["Pending project"]
    assert [row.slug for row in orgs] == ["race-operator"]


async def test_concurrent_first_requests_share_one_org(database):
    """Several sessions bootstrapping at once produce one organization and no failure."""
    engine, _path = database
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)

    async def bootstrap() -> str:
        async with factory() as session:
            org = await ensure_org(session, "gather-operator")
            await session.commit()
            return org.slug

    slugs = await asyncio.gather(*(bootstrap() for _ in range(4)))
    assert set(slugs) == {"gather-operator"}

    async with factory() as session:
        rows = (await session.execute(select(Organization))).scalars().all()
    assert [row.slug for row in rows] == ["gather-operator"]


async def test_ensure_org_is_idempotent(database):
    engine, _path = database
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with factory() as session:
        first = await ensure_org(session, "keep-operator")
        second = await ensure_org(session, "keep-operator")
        assert first.id == second.id
        assert session.is_active, "a repeat call must not disturb the caller's session"
        await session.commit()
