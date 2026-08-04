from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy import String, text
from sqlalchemy.orm import Mapped, mapped_column

from data_sources.config.schema import ConnectorConfig, StoreConfig
from data_sources.core.connector import Connector
from data_sources.core.exceptions import ConfigurationError, StoreNotFoundError
from data_sources.core.models import Item, ItemType
from data_sources.core.registry import register_connector
from data_sources.store import Base, Store, create_store, init_connector, init_store
from data_sources.store.registry import StoreRegistry, register_store

if TYPE_CHECKING:
    from sqlalchemy.orm import DeclarativeBase


class DummyStore(Store):
    driver = "dummy"

    def __init__(self, config: StoreConfig) -> None:
        super().__init__(config)
        self.connected = False
        self.migrated = False
        self.ensured_models: list[Sequence[type[DeclarativeBase]]] = []

    async def connect(self) -> None:
        self.connected = True

    async def migrate(self) -> None:
        self.migrated = True

    async def ensure_tables(self, models: Sequence[type[DeclarativeBase]]) -> None:
        self.ensured_models.append(models)

    @asynccontextmanager
    async def session(self) -> AsyncIterator[Any]:
        yield {"connected": self.connected}

    async def close(self) -> None:
        self.connected = False


class Widget(Base):
    __tablename__ = "test_store_widget"

    id: Mapped[str] = mapped_column(String(255), primary_key=True)
    name: Mapped[str] = mapped_column(String(255))


@register_connector("widget-dummy")
class WidgetConnector(Connector):
    provider = "widget-dummy"
    models = (Widget,)

    async def connect(self) -> None:
        pass

    async def validate(self) -> bool:
        return True

    async def list(
        self, path: str | None = None, *, recursive: bool = False
    ) -> AsyncIterator[Item]:
        yield Item(id="1", name="widget.txt", type=ItemType.FILE)

    async def get_metadata(self, item_id: str) -> Item:
        return Item(id=item_id, name="widget.txt", type=ItemType.FILE)

    async def download(self, item: Item) -> AsyncIterator[bytes]:
        yield b"content"

    async def close(self) -> None:
        pass


@pytest.mark.asyncio
async def test_init_store_defaults_to_sqlite(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    config = StoreConfig(url=f"sqlite+aiosqlite:///{db_path}")

    store = await init_store(config)
    try:
        assert store.driver == "sqlite"
        async with store.session() as session:
            result = await session.execute(text("select 1"))
            assert result.scalar_one() == 1
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_postgresql_driver_without_url_raises_on_connect() -> None:
    store = create_store(StoreConfig(driver="postgresql"))

    with pytest.raises(ConfigurationError):
        await store.connect()


def test_create_store_unknown_driver_raises() -> None:
    with pytest.raises(StoreNotFoundError):
        create_store(StoreConfig(driver="unknown"))


@pytest.mark.asyncio
async def test_register_store_decorator_and_custom_driver() -> None:
    registry = StoreRegistry()
    registry.register("dummy", DummyStore)

    store = registry.create(StoreConfig(driver="dummy"))
    assert isinstance(store, DummyStore)

    await store.connect()
    await store.migrate()
    assert store.connected
    assert store.migrated

    async with store.session() as session:
        assert session == {"connected": True}

    await store.close()
    assert not store.connected


def test_register_store_decorator() -> None:
    @register_store("decorated-dummy")
    class DecoratedDummy(DummyStore):
        driver = "decorated-dummy"

    store = create_store(StoreConfig(driver="decorated-dummy"))

    assert isinstance(store, DecoratedDummy)


@pytest.mark.asyncio
async def test_registering_a_connector_does_not_touch_the_store() -> None:
    # `WidgetConnector` was already registered at import time via `@register_connector`,
    # decorator-time registration alone must never provision any schema.
    store = DummyStore(StoreConfig(driver="dummy"))

    assert store.ensured_models == []


@pytest.mark.asyncio
async def test_init_connector_ensures_declared_tables_at_creation_time() -> None:
    store = DummyStore(StoreConfig(driver="dummy"))

    connector = await init_connector(ConnectorConfig(provider="widget-dummy"), store)

    assert isinstance(connector, WidgetConnector)
    assert store.ensured_models == [(Widget,)]


@pytest.mark.asyncio
async def test_init_connector_creates_real_tables_in_sqlite(tmp_path: Path) -> None:
    db_path = tmp_path / "widgets.db"
    store = await init_store(StoreConfig(url=f"sqlite+aiosqlite:///{db_path}"))
    try:
        async with store.session() as session:
            before = await session.execute(
                text("select name from sqlite_master where type='table' and name=:name"),
                {"name": Widget.__tablename__},
            )
            assert before.scalar_one_or_none() is None

        await init_connector(ConnectorConfig(provider="widget-dummy"), store)

        async with store.session() as session:
            after = await session.execute(
                text("select name from sqlite_master where type='table' and name=:name"),
                {"name": Widget.__tablename__},
            )
            assert after.scalar_one() == Widget.__tablename__
    finally:
        await store.close()
