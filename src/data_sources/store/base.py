from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from contextlib import AbstractAsyncContextManager
from types import TracebackType
from typing import TYPE_CHECKING, ClassVar

from data_sources.config.schema import StoreConfig

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.orm import DeclarativeBase


class Store(ABC):
    """Base class every storage backend implements."""

    driver: ClassVar[str]

    def __init__(self, config: StoreConfig) -> None:
        self.config = config

    async def __aenter__(self) -> Store:
        await self.connect()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    @abstractmethod
    async def connect(self) -> None:
        """Acquire the engine/connection pool used to talk to the backing database."""

    @abstractmethod
    async def migrate(self) -> None:
        """Apply pending migrations up to head."""

    @abstractmethod
    async def ensure_tables(self, models: Sequence[type[DeclarativeBase]]) -> None:
        """Create any tables/indexes declared by `models` that don't already exist.

        Idempotent and scoped to `models` — unlike `migrate`, this doesn't touch Alembic's
        revision history. Intended for connectors that declare their own persistence needs
        and should get a working schema as soon as they're instantiated (see `init_connector`).
        """

    @abstractmethod
    def session(self) -> AbstractAsyncContextManager[AsyncSession]:
        """Open a session-scoped unit of work."""

    @abstractmethod
    async def close(self) -> None:
        """Release the engine/connection pool acquired in `connect`."""
