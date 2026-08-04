from __future__ import annotations

from typing import ClassVar

from data_sources.store.registry import register_store
from data_sources.store.sqlalchemy_store import SQLAlchemyStore


@register_store("postgresql")
class PostgreSQLStore(SQLAlchemyStore):
    """PostgreSQL-backed store. Requires an explicit `url` in `StoreConfig`, e.g.

        StoreConfig(driver="postgresql", url="postgresql+asyncpg://user:pass@host/db")

    Install the `postgresql` extra to pull in the `asyncpg` driver.
    """

    driver: ClassVar[str] = "postgresql"
