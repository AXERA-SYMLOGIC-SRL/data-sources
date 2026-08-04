from __future__ import annotations

from collections.abc import Callable

from data_sources.config.schema import StoreConfig
from data_sources.core.exceptions import StoreNotFoundError
from data_sources.store.base import Store


class StoreRegistry:
    """Maps driver names to `Store` implementations."""

    def __init__(self) -> None:
        self._stores: dict[str, type[Store]] = {}

    def register(self, driver: str, store_cls: type[Store]) -> None:
        self._stores[driver] = store_cls

    def unregister(self, driver: str) -> None:
        self._stores.pop(driver, None)

    def get(self, driver: str) -> type[Store]:
        try:
            return self._stores[driver]
        except KeyError as exc:
            raise StoreNotFoundError(f"No store registered for driver '{driver}'") from exc

    def create(self, config: StoreConfig) -> Store:
        store_cls = self.get(config.driver)
        return store_cls(config)

    def drivers(self) -> list[str]:
        return sorted(self._stores)


registry = StoreRegistry()


def register_store(driver: str) -> Callable[[type[Store]], type[Store]]:
    """Class decorator that registers a `Store` implementation under `driver`."""

    def decorator(store_cls: type[Store]) -> type[Store]:
        registry.register(driver, store_cls)
        return store_cls

    return decorator


def create_store(config: StoreConfig | None = None) -> Store:
    """Instantiate the store registered for `config.driver` (SQLite by default)."""
    return registry.create(config or StoreConfig())


async def init_store(config: StoreConfig | None = None) -> Store:
    """Create, connect, and migrate a store in one call — the common entrypoint."""
    store = create_store(config)
    await store.connect()
    await store.migrate()
    return store
