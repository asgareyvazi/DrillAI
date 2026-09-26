"""Async engine/session management.

One place creates engines and sessions so that connection handling, transaction policy
and test isolation are consistent:

* the API uses ``Database.session()`` per request (commit on success, rollback on error);
* workflow runs and background jobs open their own short-lived sessions;
* tests can point at SQLite in-memory (fast) or a real PostgreSQL instance
  (``tests/integration``) without changing application code.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from typing import Any

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from drillai.core.config import Settings, get_settings
from drillai.core.logging import get_logger

logger = get_logger(__name__)


class Database:
    """Owns an async engine and session factory."""

    def __init__(self, url: str | None = None, *, echo: bool | None = None, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.url = url or self.settings.sqlalchemy_url()
        self.echo = self.settings.db_echo if echo is None else echo
        self._engine: AsyncEngine | None = None
        self._session_factory: async_sessionmaker[AsyncSession] | None = None

    # ------------------------------------------------------------------ lifecycle
    @property
    def engine(self) -> AsyncEngine:
        if self._engine is None:
            self._engine = self._create_engine()
        return self._engine

    def _create_engine(self) -> AsyncEngine:
        kwargs: dict[str, Any] = {"echo": self.echo, "pool_pre_ping": True, "future": True}
        if self.url.startswith("sqlite"):
            # SQLite (tests/dev): a single connection shared across tasks.
            kwargs["connect_args"] = {"check_same_thread": False}
            if ":memory:" in self.url:
                from sqlalchemy.pool import StaticPool

                kwargs["poolclass"] = StaticPool
            engine = create_async_engine(self.url, **kwargs)
            # SQLite needs explicit FKs ON to behave like PostgreSQL.
            event.listen(engine.sync_engine, "connect", _sqlite_fk_pragma)
            return engine
        kwargs["pool_size"] = self.settings.db_pool_size
        kwargs["max_overflow"] = self.settings.db_max_overflow
        return create_async_engine(self.url, **kwargs)

    @property
    def session_factory(self) -> async_sessionmaker[AsyncSession]:
        if self._session_factory is None:
            self._session_factory = async_sessionmaker(
                bind=self.engine, expire_on_commit=False, autoflush=False
            )
        return self._session_factory

    @contextlib.asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """Session scope with commit/rollback semantics."""
        session = self.session_factory()
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

    async def create_all(self) -> None:
        from drillai.db.models import Base

        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def drop_all(self) -> None:
        from drillai.db.models import Base

        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)

    async def dispose(self) -> None:
        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None
            self._session_factory = None

    async def healthcheck(self) -> dict[str, Any]:
        """Readiness probe: proves the database is reachable and queryable."""
        try:
            async with self.engine.connect() as conn:
                result = await conn.execute(text("SELECT 1"))
                result.scalar_one()
            return {"status": "ok", "dialect": self.engine.dialect.name}
        except Exception as exc:
            logger.warning("database healthcheck failed", extra={"extra_fields": {"error": str(exc)}})
            return {"status": "error", "dialect": self.engine.dialect.name, "error": str(exc)}

    def dialect_name(self) -> str:
        return self.engine.dialect.name


def _sqlite_fk_pragma(dbapi_connection, _connection_record) -> None:  # pragma: no cover - driver hook
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()
