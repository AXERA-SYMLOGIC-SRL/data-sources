from __future__ import annotations

import builtins
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from types import TracebackType
from typing import ClassVar

from data_sources.config.schema import ConnectorConfig
from data_sources.core.exceptions import UnsupportedOperationError
from data_sources.core.models import Change, Item, Permission, SyncCursor


class Connector(ABC):
    """Base class every provider-specific connector implements."""

    provider: ClassVar[str]

    supports_sync: ClassVar[bool] = False
    supports_permissions: ClassVar[bool] = False
    supports_webhooks: ClassVar[bool] = False

    def __init__(self, config: ConnectorConfig) -> None:
        self.config = config

    async def __aenter__(self) -> Connector:
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
        """Acquire any resources needed to talk to the provider (sessions, tokens, clients)."""

    @abstractmethod
    async def validate(self) -> bool:
        """Verify that the current configuration can authenticate and reach the provider."""

    @abstractmethod
    def list(self, path: str | None = None, *, recursive: bool = False) -> AsyncIterator[Item]:
        """List items under `path` (provider root if omitted)."""

    @abstractmethod
    async def get_metadata(self, item_id: str) -> Item:
        """Fetch metadata for a single item by its provider-specific id."""

    @abstractmethod
    def download(self, item: Item) -> AsyncIterator[bytes]:
        """Stream the content of `item`."""

    def sync(self, cursor: SyncCursor | None = None) -> AsyncIterator[Change]:
        """Yield changes since `cursor`. Only available when `supports_sync` is True."""
        raise UnsupportedOperationError(f"{self.provider} does not support incremental sync")

    async def get_permissions(self, item: Item) -> builtins.list[Permission]:
        """Return the permissions on `item`. Only available when `supports_permissions` is True."""
        raise UnsupportedOperationError(f"{self.provider} does not support permission retrieval")

    @abstractmethod
    async def close(self) -> None:
        """Release any resources acquired in `connect`."""
