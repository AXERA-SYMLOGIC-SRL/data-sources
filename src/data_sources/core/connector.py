from __future__ import annotations

import builtins
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from types import TracebackType
from typing import TYPE_CHECKING, Any, ClassVar

from data_sources.config.schema import ConnectorConfig
from data_sources.core.exceptions import UnsupportedOperationError
from data_sources.core.models import Change, ChangeType, Item, Permission, Subscription, SyncCursor

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

    async def create_webhook(self, notification_url: str) -> Subscription:
        """Subscribe to change notifications for this connector's resource.

        `notification_url` is where the provider will POST notifications; delivering
        them to `sync()`/`sync_in_background` is the caller's job, not the connector's.
        Only available when `supports_webhooks` is True.
        """
        raise UnsupportedOperationError(f"{self.provider} does not support webhooks")

    async def renew_webhook(self, subscription_id: str) -> Subscription:
        """Extend a subscription created by `create_webhook` past its expiration.

        Only available when `supports_webhooks` is True.
        """
        raise UnsupportedOperationError(f"{self.provider} does not support webhooks")

    async def delete_webhook(self, subscription_id: str) -> None:
        """Cancel a subscription created by `create_webhook`.

        Only available when `supports_webhooks` is True.
        """
        raise UnsupportedOperationError(f"{self.provider} does not support webhooks")

    async def list_webhooks(self) -> builtins.list[Subscription]:
        """List this connector's own active subscriptions at the provider.

        Only available when `supports_webhooks` is True.
        """
        raise UnsupportedOperationError(f"{self.provider} does not support webhooks")

    async def verify_webhook_notification(self, payload: dict[str, Any]) -> bool:
        """Check whether an inbound webhook POST body actually came from a subscription
        this connector itself created via `create_webhook` — e.g. by comparing a secret
        generated at creation time against one embedded in `payload`.

        Unlike the other optional webhook methods, the default is to reject (`False`)
        rather than raise: a connector that claims `supports_webhooks` but doesn't
        override this would otherwise leave every caller trusting unverified payloads.
        """
        return False

    async def get_permissions(self, item: Item) -> builtins.list[Permission]:
        """Return the permissions on `item`. Only available when `supports_permissions` is True."""
        raise UnsupportedOperationError(f"{self.provider} does not support permission retrieval")

    @abstractmethod
    async def close(self) -> None:
        """Release any resources acquired in `connect`."""
