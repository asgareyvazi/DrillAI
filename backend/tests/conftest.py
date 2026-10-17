"""Shared test fixtures.

Two persistence modes are supported:

* **SQLite in-memory** (default) — fast unit/API tests.
* **PostgreSQL** — set ``DRILLAI_TEST_POSTGRES=1`` to start an embedded PostgreSQL via the
  ``pgserver`` package. Tests marked ``postgres`` are skipped when it is unavailable.
  SQLite alone cannot prove JSON/UUID/pooling behaviour, so the critical persistence
  paths are exercised against real PostgreSQL as well.
"""

from __future__ import annotations

import os
import shutil
import tempfile

try:
    import resource
except ImportError:  # pragma: no cover - Windows does not provide POSIX resource module
    resource = None  # type: ignore[assignment]

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from drillai.core.clock import FixedClock
from drillai.db.models import Base
from drillai.db.session import dispose_all_databases


def pytest_configure(config: pytest.Config) -> None:
    """Ensure sufficient file descriptor budget across 1,000+ async SQLite/PostgreSQL tests."""
    if resource is None:
        return
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        target = min(65536, hard) if hard > 0 else 65536
        if soft < target:
            resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
    except (ValueError, OSError):
        pass


@pytest_asyncio.fixture(autouse=True)
async def _dispose_leaked_databases(request: pytest.FixtureRequest):
    yield
    await dispose_all_databases()
    if "tmp_path" in request.fixturenames:
        tmp_dir = request.node.funcargs.get("tmp_path")
        if tmp_dir is not None:
            shutil.rmtree(tmp_dir, ignore_errors=True)


@pytest.fixture(scope="session")
def postgres_url():
    """Embedded PostgreSQL for integration tests (opt-in, session scoped)."""
    if os.environ.get("DRILLAI_TEST_POSTGRES", "").lower() not in {"1", "true", "yes"}:
        pytest.skip("set DRILLAI_TEST_POSTGRES=1 to run PostgreSQL integration tests")
    try:
        import pgserver
    except ImportError:  # pragma: no cover - optional dependency
        pytest.skip("pgserver is not installed")

    data_dir = tempfile.mkdtemp(prefix="drillai-pg-")
    server = pgserver.get_server(data_dir)
    url = server.get_uri().replace("postgresql://", "postgresql+asyncpg://")
    try:
        yield url
    finally:
        server.cleanup()


@pytest_asyncio.fixture
async def pg_engine(postgres_url):
    engine = create_async_engine(postgres_url, pool_size=2, max_overflow=0)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def pg_session(pg_engine) -> AsyncSession:
    factory = async_sessionmaker(bind=pg_engine, expire_on_commit=False, autoflush=False)
    async with factory() as sess:
        yield sess


@pytest_asyncio.fixture
async def engine():
    """In-memory SQLite engine with the full schema created."""
    eng = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def session(engine) -> AsyncSession:
    factory = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    async with factory() as sess:
        yield sess


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock()
