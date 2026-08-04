from __future__ import annotations

from collections.abc import Callable

from data_sources.config.schema import ConnectorConfig
from data_sources.core.connector import Connector
from data_sources.core.exceptions import ConnectorNotFoundError


class ConnectorRegistry:
    """Maps provider names to `Connector` implementations."""

    def __init__(self) -> None:
        self._connectors: dict[str, type[Connector]] = {}

    def register(self, provider: str, connector_cls: type[Connector]) -> None:
        self._connectors[provider] = connector_cls

    def unregister(self, provider: str) -> None:
        self._connectors.pop(provider, None)

    def get(self, provider: str) -> type[Connector]:
        try:
            return self._connectors[provider]
        except KeyError as exc:
            raise ConnectorNotFoundError(
                f"No connector registered for provider '{provider}'"
            ) from exc

    def create(self, config: ConnectorConfig) -> Connector:
        connector_cls = self.get(config.provider)
        return connector_cls(config)

    def providers(self) -> list[str]:
        return sorted(self._connectors)


registry = ConnectorRegistry()


def register_connector(provider: str) -> Callable[[type[Connector]], type[Connector]]:
    """Class decorator that registers a `Connector` implementation under `provider`."""

    def decorator(connector_cls: type[Connector]) -> type[Connector]:
        registry.register(provider, connector_cls)
        return connector_cls

    return decorator


def create_connector(config: ConnectorConfig) -> Connector:
    """Instantiate the connector registered for `config.provider`."""
    return registry.create(config)
