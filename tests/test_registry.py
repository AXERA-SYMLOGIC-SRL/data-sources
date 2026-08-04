from collections.abc import AsyncIterator

import pytest

from data_sources import ConnectorConfig, create_connector, register_connector
from data_sources.core import Connector, ConnectorNotFoundError, Item, ItemType
from data_sources.core.registry import ConnectorRegistry


class DummyConnector(Connector):
    provider = "dummy"

    async def connect(self) -> None:
        pass

    async def validate(self) -> bool:
        return True

    async def list(
        self, path: str | None = None, *, recursive: bool = False
    ) -> AsyncIterator[Item]:
        yield Item(id="1", name="file.txt", type=ItemType.FILE)

    async def get_metadata(self, item_id: str) -> Item:
        return Item(id=item_id, name="file.txt", type=ItemType.FILE)

    async def download(self, item: Item) -> AsyncIterator[bytes]:
        yield b"content"

    async def close(self) -> None:
        pass


def test_register_connector_and_create() -> None:
    registry = ConnectorRegistry()
    registry.register("dummy", DummyConnector)

    connector = registry.create(ConnectorConfig(provider="dummy"))

    assert isinstance(connector, DummyConnector)


def test_create_connector_unknown_provider_raises() -> None:
    registry = ConnectorRegistry()

    with pytest.raises(ConnectorNotFoundError):
        registry.create(ConnectorConfig(provider="unknown"))


def test_register_connector_decorator() -> None:
    @register_connector("decorated-dummy")
    class DecoratedDummy(DummyConnector):
        provider = "decorated-dummy"

    connector = create_connector(ConnectorConfig(provider="decorated-dummy"))

    assert isinstance(connector, DecoratedDummy)


@pytest.mark.asyncio
async def test_dummy_connector_list_and_download() -> None:
    connector = DummyConnector(ConnectorConfig(provider="dummy"))

    items = [item async for item in connector.list()]
    assert items[0].name == "file.txt"

    chunks = [chunk async for chunk in connector.download(items[0])]
    assert chunks == [b"content"]
