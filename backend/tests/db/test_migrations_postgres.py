"""The migration chain against a real PostgreSQL.

The other persistence tests prove the *schema*; this proves the *migrations*. The difference matters,
because a migration is code that runs once, on somebody's database, with data already in it — and the
dialects disagree about exactly the things this chain does: boolean flags (SQLite stores them as
integers and PostgreSQL refuses `boolean = integer` outright, which is how the asset-identity revision
failed the first time it ran on PostgreSQL) and string aggregation.

What is asserted, in the order a deployment would meet it:

1. the whole chain applies from empty to head;
2. `alembic check` finds no drift between what the migrations built and what the models declare;
3. the last revision reverses and re-applies cleanly (a downgrade that hands back a database the
   application cannot use is not a downgrade);
4. a database that already contains two wells sharing a regulator identifier is **refused**, with the
   offenders named, rather than silently repaired by guessing which well owns the identifier.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

BACKEND_ROOT = Path(__file__).resolve().parents[2]

#: The revision the asset-identity change sits on top of — the database state a deployment upgrading
#: into it would already have.
PREVIOUS_REVISION = "7d5e1c4a90b2"


def _config(url: str) -> Config:
    """An Alembic configuration pointed at ``url``, independent of the process environment."""
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    # The environment reads this before application settings, which is what lets the test drive the
    # chain against its own embedded server without exporting an environment variable that the rest of
    # the suite would then share.
    config.set_main_option("sqlalchemy.url", url)
    return config


async def _insert_two_duplicate_uwi_wells(url: str) -> list[str]:
    """Two wells sharing one UWI, written the way the application writes them (through the models).

    Returns the ids, so the failure message can be checked against the rows that really exist rather
    than against a string the test made up.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from drillai.db.models import Organization, Project, Well

    engine = create_async_engine(url, pool_size=2, max_overflow=0)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    try:
        async with factory() as session:
            org = Organization(slug="migration-check", name="Migration Check", kind="operator")
            session.add(org)
            await session.flush()
            project = Project(org_id=org.id, name="Migration Project")
            session.add(project)
            await session.flush()
            ids: list[str] = []
            for index in (1, 2):
                well = Well(
                    org_id=org.id,
                    project_id=project.id,
                    name=f"Legacy {index}",
                    # The pre-repair value: the migration is expected to move this to the canonical one.
                    well_type="development",
                    status="planned",
                    uwi="MIGRATION-DUP",
                )
                session.add(well)
                ids.append(well.id)
            await session.commit()
            return ids
    finally:
        await engine.dispose()


async def _well_ids(url: str) -> list[str]:
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from drillai.db.models import Well

    engine = create_async_engine(url, pool_size=2, max_overflow=0)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    try:
        async with factory() as session:
            result = await session.execute(select(Well.id).order_by(Well.id))
            return list(result.scalars())
    finally:
        await engine.dispose()


@pytest.mark.postgres
def test_migration_chain_applies_reverses_and_refuses_duplicates_on_postgres(postgres_url):
    """Synchronous on purpose: Alembic's environment runs the migrations with ``asyncio.run``, so a
    migration driven from inside a running event loop cannot work — and neither can the real
    deployment command, which is the thing being tested."""
    config = _config(postgres_url)

    # 1. Fresh database, whole chain.
    command.upgrade(config, "head")

    # 2. No drift between the migrations and the models.
    command.check(config)

    # 3. Reverse the last revision and re-apply it.
    command.downgrade(config, PREVIOUS_REVISION)
    command.upgrade(config, "head")
    command.check(config)

    # 4. An existing database with duplicate identifiers must be refused, not guessed at. The rows are
    #    written after stepping back to the previous revision — the state such a database really is in.
    command.downgrade(config, PREVIOUS_REVISION)
    inserted = asyncio.run(_insert_two_duplicate_uwi_wells(postgres_url))
    assert len(inserted) == 2

    with pytest.raises(Exception) as raised:
        command.upgrade(config, "head")
    message = str(raised.value)
    assert "duplicate values already recorded" in message
    # The message names the wells that own the clash: a migration that stops without saying which rows
    # are involved leaves a person grepping a production database.
    for well_id in asyncio.run(_well_ids(postgres_url)):
        assert well_id in message

    # Leave the database usable: the two rows are removed and the chain completes. A test that left the
    # schema half-upgraded would make the *next* test's failure look like its own.
    async def _clear() -> None:
        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

        engine = create_async_engine(postgres_url, pool_size=2, max_overflow=0)
        factory = async_sessionmaker(bind=engine, expire_on_commit=False)
        try:
            async with factory() as session:
                await session.execute(text("DELETE FROM wells"))
                await session.commit()
        finally:
            await engine.dispose()

    asyncio.run(_clear())
    command.upgrade(config, "head")
    command.check(config)


def test_the_migration_environment_honours_an_explicit_url(tmp_path):
    """The environment reads the URL the caller configured, before application settings.

    Without this, the test above would drive the *configured* database rather than its own — and the
    failure mode of that would be a migration running against whatever the environment happened to
    point at, which is the one thing a migration runner must never do.
    """
    from drillai.core.config import get_settings

    config = _config("sqlite+aiosqlite:///" + str(tmp_path / "explicit.db"))
    assert config.get_main_option("sqlalchemy.url") != get_settings().sqlalchemy_url()

    command.upgrade(config, "head")
    # The application's own database was not touched: the file the URL names is the one that was built.
    assert (tmp_path / "explicit.db").exists()
