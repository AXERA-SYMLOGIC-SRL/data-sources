from collections.abc import AsyncIterator

import pytest

from data_sources.config import ConnectorConfig
from data_sources.core import Change, ChangeType, Connector, Item, ItemType, SyncCursor
from data_sources.core.exceptions import UnsupportedOperationError


class SyncingConnector(Connector):
    """Connector whose `sync()` yields one change per persisted delta token, backed by an
    in-memory cursor instead of a real tracking table — enough to exercise `sync_in_background`
    without a `Store`."""

    provider = "syncing-dummy"
    supports_sync = True

    def __init__(self, config: ConnectorConfig) -> None:
        super().__init__(config)
        self.tokens = ["1", "2", "3"]
        self.committed_cursor: SyncCursor | None = None

    async def connect(self) -> None:
        pass

    async def validate(self) -> bool:
        return True

    async def list(
        self, path: str | None = None, *, recursive: bool = False
    ) -> AsyncIterator[Item]:
        return
        yield

    async def get_metadata(self, item_id: str) -> Item:
        return Item(id=item_id, name="file.txt", type=ItemType.FILE)

    async def download(self, item: Item) -> AsyncIterator[bytes]:
        return
        yield

    async def sync(self, cursor: SyncCursor | None = None) -> AsyncIterator[Change]:
        start = self.tokens.index(cursor.token) + 1 if cursor and cursor.token else 0
        for token in self.tokens[start:]:
            yield Change(item_id=token, type=ChangeType.UPDATED, cursor=SyncCursor(token=token))

    async def _load_cursor(self) -> SyncCursor | None:
        return self.committed_cursor

    async def _commit_cursor(self, cursor: SyncCursor) -> None:
        self.committed_cursor = cursor

    async def close(self) -> None:
        pass


@pytest.mark.asyncio
async def test_sync_in_background_delivers_changes_and_commits_cursor_after_each() -> None:
    connector = SyncingConnector(ConnectorConfig(provider="syncing-dummy"))
    delivered = []

    async def on_change(change: Change) -> None:
        delivered.append(change.item_id)

    await connector.sync_in_background(on_change)

    assert delivered == ["1", "2", "3"]
    assert connector.committed_cursor == SyncCursor(token="3")


@pytest.mark.asyncio
async def test_sync_in_background_resumes_from_committed_cursor() -> None:
    connector = SyncingConnector(ConnectorConfig(provider="syncing-dummy"))
    connector.committed_cursor = SyncCursor(token="1")
    delivered = []

    async def on_change(change: Change) -> None:
        delivered.append(change.item_id)

    await connector.sync_in_background(on_change)

    assert delivered == ["2", "3"]


@pytest.mark.asyncio
async def test_sync_in_background_does_not_commit_past_a_failed_on_change() -> None:
    connector = SyncingConnector(ConnectorConfig(provider="syncing-dummy"))

    async def on_change(change: Change) -> None:
        if change.item_id == "2":
            raise RuntimeError("downstream handler blew up")

    with pytest.raises(RuntimeError):
        await connector.sync_in_background(on_change)

    # "1" was delivered and committed; "2" failed before its cursor could commit, so a
    # retry redelivers it rather than skipping ahead to "3".
    assert connector.committed_cursor == SyncCursor(token="1")


@pytest.mark.asyncio
async def test_sync_in_background_requires_sync_support() -> None:
    class NoSyncConnector(Connector):
        provider = "no-sync-dummy"

        async def connect(self) -> None:
            pass

        async def validate(self) -> bool:
            return True

        async def list(
            self, path: str | None = None, *, recursive: bool = False
        ) -> AsyncIterator[Item]:
            return
            yield

        async def get_metadata(self, item_id: str) -> Item:
            return Item(id=item_id, name="file.txt", type=ItemType.FILE)

        async def download(self, item: Item) -> AsyncIterator[bytes]:
            return
            yield

        async def close(self) -> None:
            pass

    connector = NoSyncConnector(ConnectorConfig(provider="no-sync-dummy"))

    async def on_change(change: Change) -> None:
        pass

    with pytest.raises(UnsupportedOperationError):
        await connector.sync_in_background(on_change)


class ItemLookupConnector(Connector):
    """Connector whose `sync()` yields a create followed by a delete, backed by an
    in-memory item index instead of a real tracking table — enough to exercise
    `sync_in_background`'s item-lookup wiring without a `Store`."""

    provider = "item-lookup-dummy"
    supports_sync = True
    supports_item_lookup = True

    def __init__(self, config: ConnectorConfig) -> None:
        super().__init__(config)
        self.saved_items: dict[str, Item] = {}
        self.committed_cursor: SyncCursor | None = None

    async def connect(self) -> None:
        pass

    async def validate(self) -> bool:
        return True

    async def list(
        self, path: str | None = None, *, recursive: bool = False
    ) -> AsyncIterator[Item]:
        return
        yield

    async def get_metadata(self, item_id: str) -> Item:
        return Item(id=item_id, name="file.txt", type=ItemType.FILE)

    async def download(self, item: Item) -> AsyncIterator[bytes]:
        return
        yield

    async def sync(self, cursor: SyncCursor | None = None) -> AsyncIterator[Change]:
        item = Item(id="1", name="file.txt", type=ItemType.FILE)
        yield Change(item_id="1", type=ChangeType.CREATED, item=item, cursor=SyncCursor(token="1"))
        yield Change(item_id="2", type=ChangeType.DELETED, cursor=SyncCursor(token="2"))

    async def _load_cursor(self) -> SyncCursor | None:
        return self.committed_cursor

    async def _commit_cursor(self, cursor: SyncCursor) -> None:
        self.committed_cursor = cursor

    async def get_item(self, item_id: str) -> Item | None:
        return self.saved_items.get(item_id)

    async def _save_item(self, item: Item) -> None:
        self.saved_items[item.id] = item

    async def _delete_item(self, item_id: str) -> None:
        self.saved_items.pop(item_id, None)

    async def close(self) -> None:
        pass


@pytest.mark.asyncio
async def test_sync_in_background_saves_and_deletes_items_when_supported() -> None:
    connector = ItemLookupConnector(ConnectorConfig(provider="item-lookup-dummy"))
    connector.saved_items["2"] = Item(id="2", name="stale.txt", type=ItemType.FILE)

    async def on_change(change: Change) -> None:
        pass

    await connector.sync_in_background(on_change)

    assert await connector.get_item("1") == Item(id="1", name="file.txt", type=ItemType.FILE)
    assert await connector.get_item("2") is None


@pytest.mark.asyncio
async def test_sync_in_background_skips_item_persistence_when_unsupported() -> None:
    # `SyncingConnector` doesn't override _save_item/_delete_item; if sync_in_background
    # called them unconditionally (rather than gating on supports_item_lookup) this would
    # raise UnsupportedOperationError instead of completing normally.
    connector = SyncingConnector(ConnectorConfig(provider="syncing-dummy"))

    async def on_change(change: Change) -> None:
        pass

    await connector.sync_in_background(on_change)

    with pytest.raises(UnsupportedOperationError):
        await connector.get_item("1")
