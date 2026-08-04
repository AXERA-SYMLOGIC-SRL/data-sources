from __future__ import annotations

from typing import ClassVar

from data_sources.store.registry import register_store
from data_sources.store.sqlalchemy_store import SQLAlchemyStore


@register_store("sqlite")
class SQLiteStore(SQLAlchemyStore):
    """Default store backend — a local SQLite database, no external server required."""

    driver: ClassVar[str] = "sqlite"
    default_url: ClassVar[str | None] = "sqlite+aiosqlite:///./data_sources.db"
