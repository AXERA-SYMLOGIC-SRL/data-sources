from __future__ import annotations

import builtins
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from types import TracebackType
from typing import TYPE_CHECKING, ClassVar

from data_sources.config.schema import ConnectorConfig
from data_sources.core.exceptions import UnsupportedOperationError
from data_sources.core.models import Change, ChangeType, Item, Permission, SyncCursor

if TYPE_CHECKING:
    from sqlalchemy.orm import DeclarativeBase

    from data_sources.store.base import Store


class Connector(ABC):
    """Base class every provider-specific connector implements."""

    provider: ClassVar[str]

    supports_sync: ClassVar[bool] = False
    supports_permissions: ClassVar[bool] = False
    supports_webhooks: ClassVar[bool] = False
    supports_item_lookup: ClassVar[bool] = False

    #: ORM models (subclasses of `data_sources.store.models.Base`) this connector persists.
    #: Declared per-class; a `Store` creates their tables/indexes when the connector is
    #: instantiated via `init_connector`, not when the class is registered.
    models: ClassVar[Sequence[type[DeclarativeBase]]] = ()

    def __init__(self, config: ConnectorConfig) -> None:
        self.config = config
        #: Set by `init_connector` once the store has ensured `self.models` exist. Connectors
        #: that override `_load_cursor`/`_commit_cursor` use this to read/write their own
        #: tracking table; connectors that don't support sync never touch it.
        self.store: Store | None = None

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

    async def _load_cursor(self) -> SyncCursor | None:
        """Load this connector's own persisted cursor, or `None` if it has never synced.

        Backed by whichever of `self.models` the connector uses to track sync state.
        Only required when `supports_sync` is True.
        """
        raise UnsupportedOperationError(f"{self.provider} does not support incremental sync")

    async def _commit_cursor(self, cursor: SyncCursor) -> None:
        """Persist `cursor` as this connector's new sync resume point.

        Only required when `supports_sync` is True.
        """
        raise UnsupportedOperationError(f"{self.provider} does not support incremental sync")

    async def sync_in_background(self, on_change: Callable[[Change], Awaitable[None]]) -> None:
        """Continuously pull changes via `sync()` and hand each one to `on_change`.

        Resumes from this connector's own persisted cursor rather than requiring the caller
        to track one — see `_load_cursor`/`_commit_cursor`. A change's cursor is only
        committed after `on_change` returns for it, so `on_change` must be idempotent: a
        crash between the two redelivers that change on the next run.

        When `supports_item_lookup` is True, each change also updates this connector's own
        item index (see `_save_item`/`_delete_item`), so a later `get_item(item_id)` call
        can resolve a synced item without a live provider call — a caller only needs to have
        kept the id (e.g. from a `Change`), not the `Item` itself.

        Runs until `sync()` is exhausted; callers that want a long-lived poll loop should
        call this repeatedly (e.g. on a timer or after a webhook ping).
        """
        cursor = await self._load_cursor()
        async for change in self.sync(cursor):
            await on_change(change)
            if self.supports_item_lookup:
                if change.type == ChangeType.DELETED:
                    await self._delete_item(change.item_id)
                elif change.item is not None:
                    await self._save_item(change.item)
            if change.cursor is not None:
                await self._commit_cursor(change.cursor)

    async def get_item(self, item_id: str) -> Item | None:
        """Look up a previously-synced item from this connector's own storage.

        Returns `None` if `item_id` was never synced (or has since been deleted), rather
        than falling back to a live provider call — callers wanting the latter should use
        `get_metadata`. Only available when `supports_item_lookup` is True.
        """
        raise UnsupportedOperationError(f"{self.provider} does not support item lookup")

    async def _save_item(self, item: Item) -> None:
        """Persist `item` so a later `get_item` call can resolve it.

        Only required when `supports_item_lookup` is True.
        """
        raise UnsupportedOperationError(f"{self.provider} does not support item lookup")

    async def _delete_item(self, item_id: str) -> None:
        """Remove a previously-persisted item, called when `sync()` yields a deletion.

        Only required when `supports_item_lookup` is True.
        """
        raise UnsupportedOperationError(f"{self.provider} does not support item lookup")

    async def get_permissions(self, item: Item) -> builtins.list[Permission]:
        """Return the permissions on `item`. Only available when `supports_permissions` is True."""
        raise UnsupportedOperationError(f"{self.provider} does not support permission retrieval")

    @abstractmethod
    async def close(self) -> None:
        """Release any resources acquired in `connect`."""
