from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import ClassVar, cast

from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import Table
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from data_sources.config.schema import StoreConfig
from data_sources.core.exceptions import ConfigurationError
from data_sources.store.base import Store

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


class SQLAlchemyStore(Store):
    """Store backend backed by a SQLAlchemy async engine, schema managed by Alembic.

    Concrete drivers (e.g. `SQLiteStore`, `PostgreSQLStore`) subclass this and only need to
    set `driver` and, optionally, `default_url` when no `StoreConfig.url` is supplied.
    """

    default_url: ClassVar[str | None] = None

    def __init__(self, config: StoreConfig) -> None:
        super().__init__(config)
        self._engine: AsyncEngine | None = None
        self._session_factory: async_sessionmaker[AsyncSession] | None = None

    @property
    def url(self) -> str:
        if self.config.url:
            return self.config.url
        if self.default_url:
            return self.default_url
        raise ConfigurationError(f"Store driver '{self.driver}' requires an explicit 'url'")

    @property
    def engine(self) -> AsyncEngine:
        if self._engine is None:
            raise ConfigurationError("Store must be connected before use")
        return self._engine

    async def connect(self) -> None:
        self._engine = create_async_engine(self.url, **self.config.options)
        self._session_factory = async_sessionmaker(self._engine, expire_on_commit=False)

    async def migrate(self) -> None:
        alembic_config = AlembicConfig()
        alembic_config.set_main_option("script_location", str(MIGRATIONS_DIR))
        alembic_config.attributes["connectable"] = self.engine
        # `command.upgrade` drives Alembic's env.py, which itself calls `asyncio.run(...)`
        # to run migrations against the async engine — run it on a separate thread so it
        # doesn't collide with the event loop we're already executing on.
        await asyncio.to_thread(command.upgrade, alembic_config, "head")

    async def ensure_tables(self, models: Sequence[type[DeclarativeBase]]) -> None:
        if not models:
            return
        tables = [cast(Table, model.__table__) for model in models]
        metadata = tables[0].metadata

        def _create(connection: Connection) -> None:
            metadata.create_all(connection, tables=tables, checkfirst=True)

        async with self.engine.begin() as connection:
            await connection.run_sync(_create)

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        if self._session_factory is None:
            raise ConfigurationError("Store must be connected before opening a session")
        async with self._session_factory() as session:
            yield session

    async def close(self) -> None:
        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None
            self._session_factory = None
