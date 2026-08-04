class DataSourceError(Exception):
    """Base class for all errors raised by data_sources."""


class ConfigurationError(DataSourceError):
    """Raised when a connector configuration is missing or invalid."""


class ConnectorNotFoundError(DataSourceError):
    """Raised when no connector is registered for a given provider."""


class StoreNotFoundError(DataSourceError):
    """Raised when no store is registered for a given driver."""


class ConnectionError(DataSourceError):
    """Raised when a connector fails to establish or validate a connection."""


class AuthenticationError(ConnectionError):
    """Raised when a connector fails to authenticate with the provider."""


class NotFoundError(DataSourceError):
    """Raised when a requested item does not exist at the provider."""


class UnsupportedOperationError(DataSourceError):
    """Raised when a connector does not implement an optional capability."""


class RateLimitError(DataSourceError):
    """Raised when a provider throttles or rate-limits a request."""
