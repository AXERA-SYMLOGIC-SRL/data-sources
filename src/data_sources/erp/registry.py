"""Finds the ERP sources: every concrete `ErpSource` subclass defined in a module under
`data_sources/erp/sources/`. Adding an ERP is adding a file there; nothing is registered by hand.
A source outside the package can still be added with `erp_sources.register`.
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil
from typing import Any

from data_sources.config.schema import ConnectorConfig
from data_sources.core.exceptions import ConnectorNotFoundError
from data_sources.erp.base import ErpSource

SOURCES = "data_sources.erp.sources"


def discover(package: str = SOURCES) -> dict[str, type[ErpSource]]:
    """Every concrete `ErpSource` subclass defined under `package`, by provider. Two sources
    claiming one provider, or a module that fails to import, raise: a broken source must not
    vanish silently.
    """
    root = importlib.import_module(package)
    found: dict[str, type[ErpSource]] = {}
    for info in pkgutil.walk_packages(root.__path__, prefix=f"{package}."):
        module = importlib.import_module(info.name)
        for _, cls in inspect.getmembers(module, inspect.isclass):
            if (
                not issubclass(cls, ErpSource)
                or inspect.isabstract(cls)
                or cls.__module__ != module.__name__
            ):
                continue
            if cls.provider in found:
                other = found[cls.provider].__qualname__
                raise RuntimeError(
                    f"Two ERP sources claim {cls.provider!r}: {other} and {cls.__qualname__}"
                )
            found[cls.provider] = cls
    return found


class ErpSourceRegistry:
    """Maps provider names to `ErpSource` classes, discovered on first use."""

    def __init__(self, package: str = SOURCES) -> None:
        self.package = package
        self._sources: dict[str, type[ErpSource]] | None = None

    @property
    def _loaded(self) -> dict[str, type[ErpSource]]:
        if self._sources is None:
            self._sources = discover(self.package)
        return self._sources

    def register(self, provider: str, source_cls: type[ErpSource]) -> None:
        self._loaded[provider] = source_cls

    def unregister(self, provider: str) -> None:
        self._loaded.pop(provider, None)

    def get(self, provider: str) -> type[ErpSource]:
        try:
            return self._loaded[provider]
        except KeyError as exc:
            raise ConnectorNotFoundError(
                f"No ERP source registered for provider '{provider}'"
            ) from exc

    def create(self, config: ConnectorConfig, **kwargs: Any) -> ErpSource:
        """The source `config.provider` names, built from `config` (`kwargs`: `transport`,
        `sleep`)."""
        return self.get(config.provider)(config, **kwargs)

    def providers(self) -> list[str]:
        return sorted(self._loaded)


erp_sources = ErpSourceRegistry()


def create_erp_source(config: ConnectorConfig, **kwargs: Any) -> ErpSource:
    """Instantiate the ERP source registered for `config.provider`."""
    return erp_sources.create(config, **kwargs)
